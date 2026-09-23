"""ROS-neutral image adapter lifecycle shared by ROS 1 and ROS 2."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time
from typing import Callable, Deque, Dict, List, Optional, Tuple

from ros_image_rtp_adapter.control_socket import SourceControlServer, SourceDescription
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
)
from ros_image_rtp_adapter.settings import AdapterSettings


LogFunction = Callable[[str], None]
EncoderFactory = Callable[..., SubprocessEncoder]


@dataclass(frozen=True)
class _QueuedFrame:
    encoder_data: bytes
    source_stamp_ns: Optional[int] = None
    jpeg_snapshot: Optional[bytes] = None
    raw_snapshot: Optional[RawFrame] = None


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
        description = SourceDescription(
            source_id=settings.source_id,
            rtp_host=settings.rtp_host,
            rtp_port=settings.rtp_port,
            width=settings.width,
            height=settings.height,
            fps=settings.fps,
            frame_id=settings.frame_id,
            snapshot_jpeg_backend=(
                "source-jpeg-passthrough"
                if settings.input_message_type == "compressed"
                else "pillow-libjpeg"
            ),
        )
        self._control = SourceControlServer(
            settings.control_socket,
            description,
            on_set_active=self.set_active,
            on_request_keyframe=self._encoder.request_keyframe,
            on_snapshot=self.snapshot_parts,
            snapshot_readback=(
                "fresh-compressed-frame"
                if settings.input_message_type == "compressed"
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
            raise

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        try:
            self._control.stop()
        finally:
            self._stop_pumps()

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
            channel.thread = None

    def _deactivate(self, channel: _EncoderChannel) -> None:
        with self._frame_condition:
            channel.active = False
            channel.pending.clear()
        # Stopping never waits for the pump: terminating the process is what
        # releases a pump blocked in a pipe write to a stalled encoder.
        channel.encoder.stop()

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
                    return
            if desired:
                with channel.lock:
                    try:
                        channel.encoder.start()
                    except Exception:
                        channel.encoder.stop()
                        raise
                    with self._frame_condition:
                        channel.active = True
                        self._frame_condition.notify_all()
            else:
                self._deactivate(channel)
        self._log_info(f"{channel.name} set-active -> {desired}")

    def submit_compressed(self, data: bytes, image_format: str, *, source_stamp_ns: Optional[int] = None) -> bool:
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
            frame = (
                require_jpeg_bytes(bytes(data))
                if self.settings.require_jpeg
                else bytes(data)
            )
        except FrameValidationError as exc:
            self._warn_validation(str(exc))
            return False
        if not frame:
            return False
        return self._enqueue(_QueuedFrame(encoder_data=frame, jpeg_snapshot=frame, source_stamp_ns=source_stamp_ns))

    def submit_raw(
        self,
        data: bytes,
        *,
        width: int,
        height: int,
        step: int,
        encoding: str,
        source_stamp_ns: Optional[int] = None,
    ) -> bool:
        if self.settings.input_message_type != "raw":
            self._warn_validation("received Image while input_message_type=compressed")
            return False
        try:
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
        return self._enqueue(_QueuedFrame(encoder_data=raw.data, raw_snapshot=raw, source_stamp_ns=source_stamp_ns))

    def _enqueue(self, frame: _QueuedFrame) -> bool:
        if self._video is not None and (frame.source_stamp_ns is None or frame.source_stamp_ns <= 0):
            self._warn_validation("H264 preview requires a valid source image timestamp")
            return False
        with self._frame_condition:
            self._latest = frame
            self._frames_in += 1
            for channel in self._channels:
                if not channel.active:
                    continue
                if len(channel.pending) == channel.pending.maxlen:
                    channel.frames_dropped += 1
                channel.pending.append(frame)
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

    def snapshot_parts(
        self,
        include_rgb: bool = True,
        require_fresh: bool = False,
    ) -> Optional[Tuple[bytes, bytes]]:
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
        if include_rgb and frame.raw_snapshot is not None:
            try:
                rgb = frame.raw_snapshot.to_rgb()
            except Exception as exc:
                self._log_error(f"raw snapshot RGB conversion failed: {exc}")
        if include_rgb and jpeg and not rgb:
            try:
                from io import BytesIO
                from PIL import Image
                rgb = Image.open(BytesIO(jpeg)).convert("RGB").tobytes()
            except Exception as exc:
                self._log_error(f"JPEG snapshot RGB conversion failed: {exc}")
        if not jpeg:
            return None
        return jpeg, rgb

    def status(self) -> Dict[str, object]:
        with self._lock:
            status: Dict[str, object] = {"frames_in": self._frames_in}
            for channel in self._channels:
                status[channel.name] = {
                    "active": channel.active,
                    "frames_out": channel.frames_out,
                    "frames_dropped": channel.frames_dropped,
                    "pending": len(channel.pending),
                }
        for channel in self._channels:
            entry = status[channel.name]
            entry["encoder_running"] = channel.encoder.running
            entry["encoder_diagnostic"] = channel.encoder.diagnostic
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
