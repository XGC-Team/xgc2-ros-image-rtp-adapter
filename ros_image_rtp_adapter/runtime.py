"""ROS-neutral image adapter lifecycle shared by ROS 1 and ROS 2."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import threading
import os
from xgc2_xrpc import Runtime, Fault, resolve_policy
import time
from typing import Callable, Deque, Dict, List, Optional, Tuple

from ros_image_rtp_adapter.control_socket import SourceControlServer, SourceDescription, SnapshotCapture
from ros_image_rtp_adapter.encoder import (
    SubprocessEncoder,
    create_h264_preview_encoder,
    create_rtp_encoder,
)
from ros_image_rtp_adapter.frames import (
    FrameValidationError,
    RawFrame,
    pack_raw_frame,
    require_jpeg_bytes,
    jpeg_geometry,
)
from ros_image_rtp_adapter.settings import (AdapterSettings, MAX_FRAME_BYTES,
                                          MAX_FRAME_PIXELS, prepare_default_control_directory)


LogFunction = Callable[[str], None]
EncoderFactory = Callable[..., SubprocessEncoder]
MAX_QUEUED_BYTES = 64 << 20


@dataclass(frozen=True)
class _QueuedFrame:
    encoder_data: bytes
    source_stamp_ns: Optional[int] = None
    jpeg_snapshot: Optional[bytes] = None
    raw_snapshot: Optional[RawFrame] = None
    frame_id: str = ""
    frame_sequence: int = 0


class _EncoderChannel:
    """One consumer-scoped encoder with its own latest-frame queue and pump.

    Media Edge (RTP) and ROS preview subscribers hold separate channels, so a
    consumer joining or leaving never restarts or throttles the other output.
    """

    def __init__(self, name: str, encoder: SubprocessEncoder, depth: int, timestamped: bool) -> None:
        self.name = name
        self.encoder = encoder
        self.timestamped = timestamped
        self.pending: Deque[_QueuedFrame] = deque(maxlen=depth)
        self.active = False
        self.frames_out = 0
        self.frames_dropped = 0
        self.pending_bytes = 0
        # Serializes consumer set-active requests for this channel.
        self.transition_lock = threading.Lock()
        # Serializes this encoder's start against its own pump writes.
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None

    def write(self, frame: _QueuedFrame) -> None:
        if self.timestamped:
            self.encoder.write_frame(frame.encoder_data, frame.source_stamp_ns)
        else:
            self.encoder.write_frame(frame.encoder_data)


class ImageRtpAdapterRuntime:
    """Own the RTP encoder, the optional ROS preview encoder, and the control socket."""

    def __init__(
        self,
        settings: AdapterSettings,
        *,
        log_info: Optional[LogFunction] = None,
        log_warning: Optional[LogFunction] = None,
        log_error: Optional[LogFunction] = None,
        encoder_factory: EncoderFactory = create_rtp_encoder,
        preview_encoder_factory: EncoderFactory = create_h264_preview_encoder,
        on_access_unit: Optional[Callable[[bytes, int], None]] = None,
    ) -> None:
        self.settings = settings
        # Only the SDK resolves XGC2_XRPC_. This composition root snapshots once
        # before allocating native encoders, then shares the policy with every
        # transport owner for this process incarnation.
        self._xrpc_policy = resolve_policy(dict(os.environ), defaults={
            "HOST_MAX_CONNECTIONS": 8, "HOST_MAX_IN_FLIGHT": 4,
            "MAX_REQUEST_BYTES": 65536,
            "MAX_RESPONSE_BYTES": MAX_FRAME_BYTES + MAX_FRAME_PIXELS * 3 + 4096,
            "CALL_TIMEOUT_MS": 10000, "HEADER_TIMEOUT_MS": 2000,
            "IDLE_TIMEOUT_MS": 10000, "SHUTDOWN_TIMEOUT_MS": 2000,
            "CLIENT_MAX_CONNECTIONS": 2, "CLIENT_MAX_REFERENCES": 1,
        }, ceilings={
            "HOST_MAX_CONNECTIONS": 8, "HOST_MAX_IN_FLIGHT": 4,
            "MAX_HEADER_BYTES": 16384, "MAX_REQUEST_BYTES": 65536,
            "MAX_RESPONSE_BYTES": MAX_FRAME_BYTES + MAX_FRAME_PIXELS * 3 + 4096,
            "CALL_TIMEOUT_MS": 10000, "CLIENT_MAX_CONNECTIONS": 2,
            "CLIENT_MAX_REFERENCES": 1,
            "HEADER_TIMEOUT_MS": 2000, "IDLE_TIMEOUT_MS": 10000,
            "SHUTDOWN_TIMEOUT_MS": 2000, "CLIENT_REFERENCE_IDLE_TIMEOUT_MS": 30000,
        }, capabilities=("diagnostics", "host", "http", "rpc", "transport",
                         "client_pool", "client_registry"), deployment_source="ros-image-rtp-adapter")
        self._encoder_factory = encoder_factory
        self._log_info = log_info or (lambda _message: None)
        self._log_warning = log_warning or (lambda _message: None)
        self._log_error = log_error or (lambda _message: None)
        depth = 1 if settings.drop_to_latest else 32
        self._encoder = encoder_factory(
            backend=settings.encoder_backend,
            **settings.encoder_kwargs(),
        )
        self._rtp = _EncoderChannel("rtp", self._encoder, depth, timestamped=False)
        self._video: Optional[_EncoderChannel] = None
        if on_access_unit is not None:
            video_encoder = preview_encoder_factory(
                backend=settings.encoder_backend,
                **settings.video_encoder_kwargs(),
            )
            video_encoder.set_access_unit_callback(on_access_unit)
            self._video = _EncoderChannel("ros-preview", video_encoder, depth, timestamped=True)
        self._channels = tuple(
            channel for channel in (self._rtp, self._video) if channel is not None
        )
        self._lock = threading.Lock()
        self._frame_condition = threading.Condition(self._lock)
        self._latest: Optional[_QueuedFrame] = None
        self._started = False
        self._pump_stop = threading.Event()
        self._frames_in = 0
        self._last_validation_warning = 0.0
        self._description = SourceDescription(
            source_id=settings.source_id,
            rtp_host=settings.rtp_host,
            rtp_port=settings.rtp_port,
            width=settings.width,
            height=settings.height,
            fps=settings.fps,
            frame_id=settings.frame_id,
            timestamp_clock_domain=settings.source_clock_domain,
            snapshot_jpeg_backend=(
                "source-jpeg-passthrough"
                if settings.input_message_type == "compressed"
                else "pillow-libjpeg"
            ),
        )
        self._rpc_runtime = None
        self._control = None

    def _create_control(self):
        self._rpc_runtime = Runtime(blocking_workers=4, policy=self._xrpc_policy)
        self._control = SourceControlServer(
            self.settings.control_socket,
            self._description,
            runtime=self._rpc_runtime,
            on_set_active=self.set_active,
            on_request_keyframe=self.request_keyframe,
            on_snapshot=self.snapshot_capture,
            on_status=self.status,
            on_configuration=self.configuration,
            on_configure=self.configure,
            on_mutable_fields=self.mutable_configuration_fields,
            snapshot_readback=(
                "fresh-compressed-frame"
                if self.settings.input_message_type == "compressed"
                else "fresh-raw-frame"
            ),
        )

    @property
    def encoder(self) -> SubprocessEncoder:
        return self._encoder

    @property
    def video_encoder(self) -> Optional[SubprocessEncoder]:
        return self._video.encoder if self._video is not None else None

    def start(self) -> None:
        if self._started:
            return
        # Fail Session readiness immediately for a missing binary, element, or
        # configured property, while leaving the actual encoders unallocated
        # until a consumer (Edge or a ROS subscriber) appears.
        for channel in self._channels:
            channel.encoder.preflight()
        prepare_default_control_directory(self.settings.control_socket, self.settings.source_id)
        self._create_control()
        self._pump_stop.clear()
        self._started = True
        try:
            for channel in self._channels:
                thread = threading.Thread(
                    target=self._run_channel_pump,
                    args=(channel,),
                    name=f"image-{channel.name}-encoder-pump",
                    daemon=True,
                )
                channel.thread = thread
                thread.start()
            self._control.start()
        except Exception:
            self._started = False
            self._stop_pumps()
            self._control.stop()
            self._rpc_runtime.close()
            self._control = self._rpc_runtime = None
            raise

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._control.stop()
        finally:
            self._stop_pumps()
        self._rpc_runtime.close()
        self._started = False
        self._control = self._rpc_runtime = None

    def _stop_pumps(self) -> None:
        self._pump_stop.set()
        with self._frame_condition:
            self._frame_condition.notify_all()
        # Stop encoders before joining: terminating a stalled encoder is what
        # releases a pump blocked in its pipe write.
        for channel in self._channels:
            self._deactivate(channel)
        for channel in self._channels:
            thread = channel.thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5.0)
                if thread.is_alive():
                    raise RuntimeError("encoder pump did not quiesce; runtime ownership retained")
            channel.thread = None

    def configuration(self):
        return {name: getattr(self.settings, name) for name in self.mutable_configuration_fields()}

    def configure(self, config):
        mutable = set(self.mutable_configuration_fields())
        for field in config:
            if field not in mutable:
                if field in self.settings.__dataclass_fields__:
                    raise Fault("restart_required", "%s requires source restart" % field, status=409)
                raise Fault("invalid_argument", "unknown source configuration field: %s" % field)
        if any(type(config[name]) is not int for name in ("rtp_port", "bitrate") if name in config):
            raise Fault("invalid_argument", "rtp_port and bitrate require integers")
        if "rtp_host" in config and not isinstance(config["rtp_host"], str):
            raise Fault("invalid_argument", "rtp_host requires a string")
        try:
            candidate = replace(self.settings, **config)
            candidate.validate()
        except (ValueError, TypeError) as error:
            raise Fault("invalid_argument", str(error)) from error
        with self._rtp.transition_lock, self._rtp.lock:
            with self._lock:
                if self._rtp.active or self._encoder.running:
                    raise Fault("conflict", "stop RTP source before applying configuration")
            encoder = self._encoder_factory(backend=candidate.encoder_backend, **candidate.encoder_kwargs())
            try:
                encoder.preflight()
            except Exception as error:
                encoder.stop()
                raise Fault("unavailable", "configured encoder preflight failed: %s" % error) from error
            self._encoder.stop()
            with self._frame_condition:
                self._rtp.pending.clear()
                self._rtp.pending_bytes = 0
                self._encoder = self._rtp.encoder = encoder
                self.settings = candidate

    def request_keyframe(self):
        raise Fault("unsupported", "stdin encoder uses bounded GOP; force-IDR is unsupported", status=501)

    def mutable_configuration_fields(self):
        fields = ["rtp_host", "rtp_port"]
        profile = (self.settings.ffmpeg_encoder_args_json if self.settings.encoder_backend == "ffmpeg"
                   else self.settings.gstreamer_encoder_properties_json)
        if (self.settings.encoder_backend == "ffmpeg" and profile.strip() == "[]"
                or '"@bitrate"' in profile or '"@bitrate_kbps"' in profile):
            fields.append("bitrate")
        return fields

    def _deactivate(self, channel: _EncoderChannel) -> None:
        with self._frame_condition:
            channel.active = False
            channel.pending.clear()
            channel.pending_bytes = 0
        # Stopping never waits for the pump: terminating the process is what
        # releases a pump blocked in a pipe write to a stalled encoder.
        channel.encoder.stop()
        if channel.encoder.running:
            raise RuntimeError("encoder did not stop; native ownership retained")

    def set_active(self, active: bool) -> None:
        """Media Edge demand (WebRTC viewer, recording, or snapshot)."""

        self._set_channel_active(self._rtp, active)

    def set_video_active(self, active: bool) -> None:
        """ROS demand for the H264 preview topic."""

        if self._video is not None:
            self._set_channel_active(self._video, active)

    def _set_channel_active(self, channel: _EncoderChannel, active: bool) -> None:
        desired = bool(active)
        with channel.transition_lock:
            with self._frame_condition:
                if desired == channel.active:
                    if channel.encoder.running == desired:
                        return
                    channel.active = False
            if desired:
                with channel.lock:
                    try:
                        channel.encoder.start()
                        if not channel.encoder.running:
                            raise RuntimeError("encoder did not start")
                    except Exception:
                        channel.encoder.stop()
                        raise
                    with self._frame_condition:
                        channel.active = True
                        self._frame_condition.notify_all()
            else:
                self._deactivate(channel)
        self._log_info(f"{channel.name} set-active -> {desired}")

    def submit_compressed(self, data: bytes, image_format: str, *, source_stamp_ns: Optional[int] = None,
                          frame_id: Optional[str] = None) -> bool:
        if self.settings.input_message_type != "compressed":
            self._warn_validation("received CompressedImage while input_message_type=raw")
            return False
        normalized_format = (image_format or "").strip().lower()
        if (
            self.settings.require_jpeg
            and "jpeg" not in normalized_format
            and "jpg" not in normalized_format
        ):
            self._warn_validation(
                f"CompressedImage format {image_format!r} is not JPEG"
            )
            return False
        try:
            if len(data) > MAX_FRAME_BYTES:
                raise FrameValidationError("CompressedImage exceeds bounded frame bytes")
            frame = (
                require_jpeg_bytes(bytes(data))
                if self.settings.require_jpeg
                else bytes(data)
            )
            # Both subprocess backends decode this JPEG. Bound decoded pixels
            # before it can enter their fixed input queues.
            jpeg_geometry(frame, MAX_FRAME_PIXELS)
        except FrameValidationError as exc:
            self._warn_validation(str(exc))
            return False
        if not frame:
            return False
        return self._enqueue(_QueuedFrame(encoder_data=frame, jpeg_snapshot=frame,
                                         source_stamp_ns=source_stamp_ns,
                                         frame_id=frame_id or self.settings.frame_id))

    def submit_raw(
        self,
        data: bytes,
        *,
        width: int,
        height: int,
        step: int,
        encoding: str,
        source_stamp_ns: Optional[int] = None,
        frame_id: Optional[str] = None,
    ) -> bool:
        if self.settings.input_message_type != "raw":
            self._warn_validation("received Image while input_message_type=compressed")
            return False
        try:
            if len(data) > MAX_FRAME_PIXELS * 4:
                raise FrameValidationError("Image exceeds bounded frame bytes")
            raw = pack_raw_frame(
                bytes(data),
                width=width,
                height=height,
                step=step,
                encoding=encoding,
                expected_width=self.settings.width,
                expected_height=self.settings.height,
                expected_encoding=self.settings.raw_encoding,
            )
        except FrameValidationError as exc:
            self._warn_validation(str(exc))
            return False
        return self._enqueue(_QueuedFrame(encoder_data=raw.data, raw_snapshot=raw,
                                         source_stamp_ns=source_stamp_ns,
                                         frame_id=frame_id or self.settings.frame_id))

    def _enqueue(self, frame: _QueuedFrame) -> bool:
        if len(frame.frame_id.encode("utf-8")) > 256:
            self._warn_validation("image frame_id exceeds bounded source identity")
            return False
        if frame.source_stamp_ns is not None and (type(frame.source_stamp_ns) is not int or frame.source_stamp_ns < 0):
            self._warn_validation("image source timestamp must be a non-negative integer")
            return False
        if self._video is not None and (frame.source_stamp_ns is None or frame.source_stamp_ns <= 0):
            self._warn_validation("H264 preview requires a valid source image timestamp")
            return False
        with self._frame_condition:
            self._frames_in += 1
            frame = replace(frame, frame_sequence=self._frames_in)
            self._latest = frame
            for channel in self._channels:
                if not channel.active:
                    continue
                while channel.pending and (len(channel.pending) == channel.pending.maxlen
                        or channel.pending_bytes + len(frame.encoder_data) > MAX_QUEUED_BYTES):
                    dropped = channel.pending.popleft()
                    channel.pending_bytes -= len(dropped.encoder_data)
                    channel.frames_dropped += 1
                channel.pending.append(frame)
                channel.pending_bytes += len(frame.encoder_data)
            self._frame_condition.notify_all()
        return True

    def pump(self) -> bool:
        """Deliver at most one queued frame to every active encoder."""

        delivered = False
        for channel in self._channels:
            delivered = self._pump_channel(channel) or delivered
        return delivered

    def _pump_channel(self, channel: _EncoderChannel) -> bool:
        with channel.lock:
            with self._lock:
                if not channel.active or not channel.pending:
                    return False
                frame = channel.pending.popleft()
                channel.pending_bytes -= len(frame.encoder_data)
            channel.write(frame)
        with self._lock:
            channel.frames_out += 1
        return True

    def _run_channel_pump(self, channel: _EncoderChannel) -> None:
        while not self._pump_stop.is_set():
            with self._frame_condition:
                self._frame_condition.wait_for(
                    lambda: self._pump_stop.is_set()
                    or (channel.active and bool(channel.pending))
                )
                if self._pump_stop.is_set():
                    return
            try:
                self._pump_channel(channel)
            except Exception as exc:
                self._log_error(f"{channel.name} encoder frame pump failed: {exc}")
                self._deactivate(channel)

    def snapshot_jpeg(self) -> Optional[bytes]:
        jpeg, _rgb = self.snapshot_parts(False) or (None, None)
        return jpeg

    def snapshot_parts(self, include_rgb=True, require_fresh=False):
        capture = self.snapshot_capture(include_rgb, require_fresh)
        return (capture.jpeg, capture.rgb) if capture else None

    def snapshot_capture(
        self,
        include_rgb: bool = True,
        require_fresh: bool = False,
    ) -> Optional[SnapshotCapture]:
        with self._frame_condition:
            if require_fresh:
                request_frame = self._frames_in
                deadline = time.monotonic() + max(
                    0.25,
                    min(2.0, 3.0 / float(self.settings.fps)),
                )
                while self._frames_in <= request_frame:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        return None
                    self._frame_condition.wait(remaining)
            frame = self._latest
        if frame is None:
            return None
        jpeg: Optional[bytes] = None
        rgb = b""
        if frame.jpeg_snapshot is not None:
            jpeg = frame.jpeg_snapshot
        elif frame.raw_snapshot is not None:
            try:
                jpeg = frame.raw_snapshot.to_jpeg()
            except Exception as exc:
                self._log_error(f"raw snapshot JPEG conversion failed: {exc}")
        if not jpeg:
            return None
        # Read geometry before decompression: compressed byte size alone cannot
        # bound an image decoder's allocation. The source stamp/sequence remain
        # tied to this immutable retained frame during all conversion work.
        from io import BytesIO
        from PIL import Image
        try:
            width, height = jpeg_geometry(jpeg, MAX_FRAME_PIXELS)
        except Exception as error:
            self._log_error("invalid snapshot JPEG: %s" % error)
            return None
        if include_rgb and frame.raw_snapshot is not None:
            try:
                rgb = frame.raw_snapshot.to_rgb()
            except Exception as exc:
                self._log_error(f"raw snapshot RGB conversion failed: {exc}")
        if include_rgb and jpeg and not rgb:
            try:
                with Image.open(BytesIO(jpeg)) as image:
                    rgb = image.convert("RGB").tobytes()
            except Exception as exc:
                self._log_error(f"JPEG snapshot RGB conversion failed: {exc}")
        if not jpeg:
            return None
        if include_rgb and len(rgb) != width * height * 3:
            return None
        return SnapshotCapture(jpeg, rgb, frame.source_stamp_ns or 0,
                               self.settings.source_clock_domain if frame.source_stamp_ns is not None else "unknown",
                               width, height, frame.frame_id, frame.frame_sequence)

    def status(self) -> Dict[str, object]:
        with self._lock:
            status: Dict[str, object] = {"frames_in": self._frames_in}
            for channel in self._channels:
                status[channel.name] = {
                    "active": channel.active,
                    "frames_out": channel.frames_out,
                    "frames_dropped": channel.frames_dropped,
                    "pending": len(channel.pending),
                    "pending_bytes": channel.pending_bytes,
                    "queue_byte_limit": MAX_QUEUED_BYTES,
                    "queue_frame_limit": channel.pending.maxlen,
                }
        for channel in self._channels:
            entry = status[channel.name]
            entry["encoder_running"] = channel.encoder.running
            entry["encoder_diagnostic"] = channel.encoder.diagnostic
        status["xrpc"] = {"effective_policy": self._xrpc_policy.snapshot()}
        if self._rpc_runtime is not None:
            status["xrpc"]["runtime"] = self._rpc_runtime.status()
        return status

    def status_report(self) -> List[Tuple[str, str]]:
        """ROS-neutral ``(level, message)`` status lines, one per encoder."""

        status = self.status()
        reports: List[Tuple[str, str]] = []
        for channel in self._channels:
            entry = status[channel.name]
            if not entry["active"]:
                reports.append(("info", (
                    f"{channel.name} idle source_id={self.settings.source_id} "
                    f"frames_in={status['frames_in']} "
                    f"encoder_released={not entry['encoder_running']}"
                )))
            elif not entry["encoder_running"]:
                reports.append(("error", (
                    f"{channel.name} encoder backend={self.settings.encoder_backend} "
                    f"is not running: {entry['encoder_diagnostic'] or 'no diagnostic'}"
                )))
            else:
                reports.append(("info", (
                    f"{channel.name} frames_in={status['frames_in']} "
                    f"frames_out={entry['frames_out']} frames_dropped={entry['frames_dropped']} "
                    f"pending={entry['pending']} topic={self.settings.image_topic} "
                    f"backend={self.settings.encoder_backend}"
                )))
        return reports

    def _warn_validation(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_validation_warning >= 5.0:
            self._last_validation_warning = now
            self._log_warning(message)
