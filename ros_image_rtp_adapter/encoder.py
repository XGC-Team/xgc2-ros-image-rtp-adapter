"""Pluggable image-frame to H264/RTP subprocess encoders.

The ROS-facing package is intentionally hardware-neutral.  FFmpeg is the
portable software default; GStreamer element factories and properties are
configuration so deployments can select a vendor hardware pipeline without
putting device detection or topic names in this module.
"""

from __future__ import annotations

from collections import deque
from fractions import Fraction
import json
import re
import signal
import subprocess
import threading
import time
from typing import Callable, Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ros_image_rtp_adapter.h264 import AnnexBAccessUnits

PropertyValue = Union[str, int, float, bool]
PropertyInput = Union[str, Mapping[str, PropertyValue]]
ArgumentInput = Union[str, Sequence[str]]

_ELEMENT_NAME = re.compile(r"^[A-Za-z0-9_.+-]+$")
_PROPERTY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_RAW_INPUTS: Mapping[str, Tuple[str, str, int]] = {
    "rgb8": ("rgb24", "rgb", 3),
    "bgr8": ("bgr24", "bgr", 3),
    "rgba8": ("rgba", "rgba", 4),
    "bgra8": ("bgra", "bgra", 4),
    "mono8": ("gray", "gray8", 1),
}


def _parse_properties(value: PropertyInput, parameter_name: str) -> Dict[str, PropertyValue]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"{parameter_name} must contain a JSON object: {exc}") from exc
    else:
        parsed = dict(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{parameter_name} must contain a JSON object")

    properties: Dict[str, PropertyValue] = {}
    for key, property_value in parsed.items():
        if not isinstance(key, str) or not _PROPERTY_NAME.fullmatch(key):
            raise ValueError(f"{parameter_name} contains an invalid property name: {key!r}")
        if not isinstance(property_value, (str, int, float, bool)):
            raise ValueError(
                f"{parameter_name}.{key} must be a string, number, or boolean"
            )
        if isinstance(property_value, str) and "\x00" in property_value:
            raise ValueError(f"{parameter_name}.{key} must not contain NUL")
        properties[key] = property_value
    return properties


def _parse_arguments(value: ArgumentInput, parameter_name: str) -> List[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError(f"{parameter_name} must contain a JSON array: {exc}") from exc
    else:
        parsed = list(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{parameter_name} must contain a JSON array of strings")
    if any("\x00" in item for item in parsed):
        raise ValueError(f"{parameter_name} must not contain NUL")
    return parsed


def _validate_element_name(value: str, parameter_name: str) -> str:
    if not _ELEMENT_NAME.fullmatch(value):
        raise ValueError(
            f"{parameter_name} must be a GStreamer element factory name, got {value!r}"
        )
    return value


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def normalize_input_format(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "jpeg":
        return normalized
    if normalized not in _RAW_INPUTS:
        raise ValueError(
            "input_format must be one of: jpeg, " + ", ".join(_RAW_INPUTS)
        )
    return normalized


def packed_frame_bytes(input_format: str, width: int, height: int) -> int:
    normalized = normalize_input_format(input_format)
    if normalized == "jpeg":
        raise ValueError("JPEG frames are variable length")
    return int(width) * int(height) * _RAW_INPUTS[normalized][2]


class SubprocessEncoder:
    """Supervised stdin-fed encoder subprocess.

    Native ownership ends only after the child is reaped and its writers and
    output readers have exited. Lifecycle changes serialize separately from
    pipe writes, so stop can terminate a stalled child before waiting for the
    writer that the termination releases.
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stopping = False
        self._stop_generation = 0
        self._readers: List[threading.Thread] = []
        self._runtime_validated = False
        self._stderr_tail = deque(maxlen=20)

    @property
    def running(self) -> bool:
        with self._state_lock:
            proc = self._proc
            return proc is not None and proc.poll() is None

    @property
    def stopped(self) -> bool:
        """Whether all owned native work has actually quiesced.

        A dead child can still have a blocked writer or an output callback.
        ``running == False`` therefore does not establish stop completion.
        """

        with self._state_lock:
            return self._proc is None and not self._readers and not self._stopping

    @property
    def diagnostic(self) -> str:
        return "\n".join(self._stderr_tail)

    def start(self) -> None:
        # A start concurrent with stop must not queue behind it and silently
        # allocate a replacement child immediately after stop completes.
        if not self._lifecycle_lock.acquire(blocking=False):
            raise RuntimeError("encoder lifecycle transition is in progress")
        try:
            with self._state_lock:
                if self._stopping:
                    raise RuntimeError("encoder stop has not completed; retry stop first")
                proc = self._proc
                if proc is None:
                    self._launch_locked()
                    return
                if proc.poll() is None:
                    return
            self._restart_locked(proc)
        finally:
            self._lifecycle_lock.release()

    def preflight(self) -> None:
        """Validate the configured backend without allocating an encoder."""

        with self._state_lock:
            if self._runtime_validated:
                return
            self.validate_runtime()
            self._runtime_validated = True

    def stop(self) -> None:
        with self._state_lock:
            self._stopping = True
            self._stop_generation += 1
            generation = self._stop_generation
        deadline = time.monotonic() + 5.0
        if not self._lifecycle_lock.acquire(timeout=self._remaining(deadline)):
            raise RuntimeError("encoder lifecycle did not quiesce before stop deadline")
        try:
            with self._state_lock:
                proc = self._proc
            if proc is not None:
                self._finish_stop(proc, deadline)
            with self._state_lock:
                self._proc = None
                self._readers.clear()
                if self._stop_generation == generation:
                    self._stopping = False
        finally:
            self._lifecycle_lock.release()

    def write_frame(self, frame: bytes, source_stamp_ns: Optional[int] = None) -> None:
        failed_proc = None
        with self._write_lock:
            with self._state_lock:
                proc = self._proc
                if self._stopping or proc is None or proc.stdin is None:
                    return
            self._before_write(source_stamp_ns)
            try:
                remaining = memoryview(frame)
                while remaining:
                    written = proc.stdin.write(remaining)
                    if written is None or written <= 0:
                        raise BrokenPipeError("encoder input pipe stopped accepting data")
                    remaining = remaining[written:]
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                failed_proc = proc
        # Never wait for lifecycle ownership while holding the write lock:
        # stop holds lifecycle ownership while joining the released writer.
        if failed_proc is not None:
            with self._lifecycle_lock:
                self._restart_locked(failed_proc)

    def _before_write(self, source_stamp_ns: Optional[int]) -> None:
        """Hook run under the write lock before a frame reaches the live process."""

    def request_keyframe(self) -> None:
        # Stdin-driven command-line encoders expose no portable live force-IDR
        # event. Profiles configure a one-second GOP and repeated SPS/PPS, so
        # the next decoder-safe keyframe is bounded without replacing RTP SSRC.
        return

    def validate_runtime(self) -> None:
        raise NotImplementedError

    def _build_command(self) -> List[str]:
        raise NotImplementedError

    def _stdout_target(self) -> int:
        return subprocess.DEVNULL

    def _before_launch(self) -> None:
        """Hook run before a new process becomes visible to writers."""

    def _on_launched(self, proc: subprocess.Popen) -> None:
        """Hook run once ``proc`` is the current process (output readers)."""

    def _start_reader_locked(self, proc: subprocess.Popen, target, name: str) -> None:
        reader = threading.Thread(target=target, args=(proc,), daemon=True, name=name)
        self._readers.append(reader)
        reader.start()

    def _launch_locked(self) -> None:
        if not self._runtime_validated:
            self.validate_runtime()
            self._runtime_validated = True
        self._before_launch()
        proc = subprocess.Popen(
            self._build_command(),
            stdin=subprocess.PIPE,
            stdout=self._stdout_target(),
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stderr_tail.clear()
        self._proc = proc
        try:
            self._start_reader_locked(proc, self._drain_stderr, "encoder-stderr")
            self._on_launched(proc)
        except BaseException:
            # A launch hook can fail after Popen has allocated native work.
            # The caller must stop this retained child before trying again.
            self._stopping = True
            raise

    def _restart_locked(self, proc: subprocess.Popen) -> None:
        """Restart a failed current child while holding lifecycle ownership."""

        with self._state_lock:
            if self._proc is not proc or self._stopping:
                return
            generation = self._stop_generation
            self._stopping = True
        self._finish_stop(proc, time.monotonic() + 5.0)
        with self._state_lock:
            self._proc = None
            self._readers.clear()
            if self._stop_generation != generation:
                # A concurrent stop owns the next transition; do not replace
                # the child it has requested to stop.
                return
            self._stopping = False
            self._launch_locked()

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _finish_stop(self, proc: subprocess.Popen, deadline: float) -> None:
        self._stop_process(proc, deadline=deadline)
        if not self._write_lock.acquire(timeout=self._remaining(deadline)):
            raise RuntimeError("encoder writer did not quiesce before stop deadline")
        self._write_lock.release()
        for reader in self._readers:
            if reader is threading.current_thread():
                raise RuntimeError("encoder output callback cannot join its own reader")
            # Thread.start itself may have failed after ownership was recorded.
            if reader.ident is not None:
                reader.join(timeout=self._remaining(deadline))
            if reader.is_alive():
                raise RuntimeError("encoder output reader did not quiesce before stop deadline")
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                self._close_pipe(pipe)

    @staticmethod
    def _close_pipe(pipe) -> None:
        try:
            pipe.close()
        except (OSError, ValueError) as exc:
            if not pipe.closed:
                raise RuntimeError("encoder pipe could not be closed") from exc

    @staticmethod
    def _stop_process(proc: subprocess.Popen, *, deadline: float) -> None:
        # Terminate before touching the write lock or closing stdin: a pipe
        # write may be blocked inside native I/O and needs the child to exit.
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError as exc:
                if proc.poll() is None:
                    raise RuntimeError("encoder child could not be terminated") from exc
        if proc.stdin is not None:
            SubprocessEncoder._close_pipe(proc.stdin)
        try:
            proc.wait(timeout=min(3.0, SubprocessEncoder._remaining(deadline)))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError as exc:
                if proc.poll() is None:
                    raise RuntimeError("encoder child could not be killed") from exc
            try:
                proc.wait(timeout=SubprocessEncoder._remaining(deadline))
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("encoder child was not reaped before stop deadline") from exc
        except OSError as exc:
            raise RuntimeError("encoder child could not be reaped") from exc

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        try:
            for line in iter(proc.stderr.readline, b""):
                self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
        except Exception:
            pass


def _ffmpeg_validate_encoder(ffmpeg_path: str, encoder: str) -> None:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-h", f"encoder={encoder}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"FFmpeg preflight failed: {exc}") from exc
    output = result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0 or "not recognized" in output.lower():
        raise RuntimeError(f"FFmpeg encoder {encoder!r} is unavailable: {output.strip()}")


def _ffmpeg_input_arguments(
    input_format: str, width: int, height: int, fps: float
) -> List[str]:
    """Input-side arguments for complete JPEG frames or packed raw frames."""

    if input_format == "jpeg":
        # image2pipe + mjpeg accepts concatenated complete JPEG frames.
        return ["-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(fps), "-i", "pipe:0"]
    pixel_format = _RAW_INPUTS[input_format][0]
    return [
        "-f", "rawvideo", "-pixel_format", pixel_format,
        "-video_size", f"{width}x{height}", "-framerate", str(fps), "-i", "pipe:0",
    ]


def _ffmpeg_codec_arguments(
    *,
    encoder: str,
    encoder_args: Sequence[str],
    bitrate: int,
    fps: float,
    width: int,
    height: int,
    input_format: str,
) -> List[str]:
    """Codec options shared by the RTP and ROS preview outputs."""

    gop = max(1, int(round(fps)))
    if encoder_args:
        context = {
            "@bitrate": str(bitrate),
            "@bitrate_kbps": str(max(1, int(round(bitrate / 1000.0)))),
            "@fps": _format_number(fps),
            "@gop": str(gop),
            "@width": str(width),
            "@height": str(height),
        }
        return [context.get(argument, argument) for argument in encoder_args]
    if encoder == "libx264":
        return [
            "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
            "-g", str(gop), "-keyint_min", str(gop), "-bf", "0",
            "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
            "-x264-params", "repeat-headers=1:scenecut=0",
        ]
    if encoder == "h264_nvenc":
        # Bound the access-unit burst before sizing the loopback RTP receive
        # queue. A nominal average bitrate alone is not a bound: motion or a
        # scene cut can otherwise create an arbitrarily larger short burst even
        # while a static camera appears to sustain 30 Hz.
        return [
            "-preset", "p4", "-tune", "ll", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-rc", "cbr", "-multipass", "qres", "-zerolatency", "1",
            "-delay",
            # MJPEG CPU decode and NVENC can overlap across bounded surfaces.
            # Zero serializes both stages on each frame (4K falls below 30 Hz).
            "2" if input_format == "jpeg" else "0",
            "-rc-lookahead", "0", "-bf", "0",
            "-g", str(gop), "-keyint_min", str(gop),
            "-no-scenecut", "1", "-strict_gop", "1", "-forced-idr", "1",
            "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate),
        ]
    # Minimal codec-level defaults. Hardware-specific flags belong in
    # ffmpeg_encoder_args_json so no vendor assumptions leak here.
    return ["-b:v", str(bitrate), "-g", str(gop)]


class FFmpegRtpEncoder(SubprocessEncoder):
    """Portable FFmpeg RTP backend; defaults to low-latency software x264.

    This is the Media Edge (WebRTC/snapshot/recording) output only. It keeps
    the configured source geometry; the ROS preview is a separate encoder.
    """

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        rtp_host: str,
        rtp_port: int,
        width: int,
        height: int,
        fps: float,
        bitrate: int,
        input_format: str = "jpeg",
        encoder: str = "libx264",
        encoder_args: ArgumentInput = "[]",
        video_filter: str = "",
    ) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._rtp_host = rtp_host
        self._rtp_port = int(rtp_port)
        self._width = int(width)
        self._height = int(height)
        self._fps = float(fps)
        self._bitrate = int(bitrate)
        self._input_format = normalize_input_format(input_format)
        self._encoder = encoder
        self._encoder_args = _parse_arguments(encoder_args, "ffmpeg_encoder_args_json")
        self._video_filter = video_filter

    def validate_runtime(self) -> None:
        _ffmpeg_validate_encoder(self._ffmpeg_path, self._encoder)

    def _build_command(self) -> List[str]:
        video_filter = self._video_filter or (
            f"scale={self._width}:{self._height}:force_original_aspect_ratio=decrease,"
            f"pad={self._width}:{self._height}:(ow-iw)/2:(oh-ih)/2"
        )
        command = [
            self._ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-fflags", "nobuffer", "-flags", "low_delay",
            # The first frame carries every stream parameter; default probing
            # would hold back the first IDR for a newly joining viewer.
            "-probesize", "32", "-analyzeduration", "0",
        ]
        command.extend(_ffmpeg_input_arguments(self._input_format, self._width, self._height, self._fps))
        command.extend(["-an", "-vf", video_filter, "-c:v", self._encoder])
        command.extend(_ffmpeg_codec_arguments(
            encoder=self._encoder, encoder_args=self._encoder_args, bitrate=self._bitrate,
            fps=self._fps, width=self._width, height=self._height, input_format=self._input_format,
        ))
        command.extend([
            "-f", "rtp", "-payload_type", "96",
            f"rtp://{self._rtp_host}:{self._rtp_port}?pkt_size=1200",
        ])
        return command


class FFmpegH264PreviewEncoder(SubprocessEncoder):
    """ROS ``CompressedVideo`` preview output with its own geometry and budget.

    It runs only while ROS subscribers exist and never touches RTP, so the
    Media Edge stream and the preview start, stop and fail independently. A
    JPEG source that is exactly twice the preview size is decoded with the
    MJPEG decoder's half-resolution IDCT (``-lowres 1``); any other ratio is
    decoded at full size and area-averaged. Both are edge-aligned downscales
    (preview pixel centre ``c' = (c + 0.5) * s - 0.5``), so a CameraInfo for
    the source still maps the full preview texture onto the same image plane.
    """

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        source_width: int,
        source_height: int,
        width: int,
        height: int,
        fps: float,
        bitrate: int,
        input_format: str = "jpeg",
        encoder: str = "libx264",
        encoder_args: ArgumentInput = "[]",
    ) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._source_width = int(source_width)
        self._source_height = int(source_height)
        self._width = int(width)
        self._height = int(height)
        self._fps = float(fps)
        self._bitrate = int(bitrate)
        self._input_format = normalize_input_format(input_format)
        self._encoder = encoder
        self._encoder_args = _parse_arguments(encoder_args, "ffmpeg_encoder_args_json")
        self._access_unit_callback: Optional[Callable[[bytes, int], None]] = None
        self._source_stamps: deque = deque()
        self._metadata_lock = threading.Lock()

    @property
    def half_resolution_decode(self) -> bool:
        return (
            self._input_format == "jpeg"
            and self._source_width == 2 * self._width
            and self._source_height == 2 * self._height
        )

    def set_access_unit_callback(self, callback: Callable[[bytes, int], None]) -> None:
        if self._proc is not None:
            raise RuntimeError("configure H264 preview before starting the encoder")
        self._access_unit_callback = callback

    def validate_runtime(self) -> None:
        _ffmpeg_validate_encoder(self._ffmpeg_path, self._encoder)

    def write_frame(self, frame: bytes, source_stamp_ns: Optional[int] = None) -> None:
        if source_stamp_ns is None or source_stamp_ns <= 0:
            raise ValueError("H264 preview requires the source image timestamp")
        super().write_frame(frame, source_stamp_ns)

    def _before_write(self, source_stamp_ns: Optional[int]) -> None:
        with self._metadata_lock:
            if len(self._source_stamps) >= 64:
                raise RuntimeError("H264 encoder output stalled")
            self._source_stamps.append(source_stamp_ns)

    def _stdout_target(self) -> int:
        return subprocess.PIPE

    def _before_launch(self) -> None:
        with self._metadata_lock:
            self._source_stamps.clear()

    def _on_launched(self, proc: subprocess.Popen) -> None:
        self._start_reader_locked(proc, self._drain_access_units, "h264-preview-output")

    def _build_command(self) -> List[str]:
        command = [
            self._ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts", "-flags", "low_delay",
            "-probesize", "32", "-analyzeduration", "0", "-threads", "1",
        ]
        if self.half_resolution_decode:
            command.extend(["-lowres", "1"])
        command.extend(_ffmpeg_input_arguments(
            self._input_format, self._source_width, self._source_height, self._fps,
        ))
        # Exact output geometry, never padding: letterbox bars would shift the
        # image inside the CameraInfo plane that Lichtblick maps the texture to.
        command.extend([
            "-an", "-vf", f"scale={self._width}:{self._height}:flags=area",
            "-c:v", self._encoder,
        ])
        command.extend(_ffmpeg_codec_arguments(
            encoder=self._encoder, encoder_args=self._encoder_args, bitrate=self._bitrate,
            fps=self._fps, width=self._width, height=self._height, input_format=self._input_format,
        ))
        command.extend([
            "-xerror", "-vsync", "0", "-bf", "0", "-map", "0:v:0",
            "-bsf:v", "dump_extra=freq=keyframe,h264_metadata=aud=insert",
            "-flush_packets", "1", "-f", "h264", "pipe:1",
        ])
        return command

    def _drain_access_units(self, proc: subprocess.Popen) -> None:
        parser = AnnexBAccessUnits()

        def emit(unit):
            with self._metadata_lock:
                if self._proc is not proc or self._stopping:
                    return
                if not self._source_stamps:
                    raise RuntimeError("H264 output has no matching source timestamp")
                stamp = self._source_stamps.popleft()
            callback = self._access_unit_callback
            if callback is not None:
                callback(unit, stamp)

        try:
            while self._proc is proc and not self._stopping:
                data = proc.stdout.read(65536)
                if not data:
                    break
                for unit in parser.feed(data):
                    emit(unit)
            for unit in parser.finish():
                emit(unit)
        except Exception as exc:
            self._stderr_tail.append(f"H264 preview output failed: {exc}")
            if proc.poll() is None:
                proc.kill()


class GStreamerRtpEncoder(SubprocessEncoder):
    """Configurable GStreamer backend for software or hardware elements."""

    def __init__(
        self,
        *,
        gstreamer_path: str,
        gstreamer_inspect_path: str,
        rtp_host: str,
        rtp_port: int,
        width: int,
        height: int,
        fps: float,
        bitrate: int,
        input_format: str = "jpeg",
        jpeg_parser: str = "jpegparse",
        jpeg_caps: str = "image/jpeg,framerate=@fps_fraction",
        jpeg_decoder: str = "jpegdec",
        video_converter: str = "videoconvert",
        video_scaler: str = "videoscale",
        raw_caps: str = (
            "video/x-raw,format=I420,width=@width,height=@height,framerate=@fps_fraction"
        ),
        h264_encoder: str = "x264enc",
        decoder_properties: PropertyInput = "{}",
        converter_properties: PropertyInput = "{}",
        encoder_properties: PropertyInput = (
            '{"bitrate":"@bitrate_kbps","byte-stream":true,'
            '"key-int-max":"@gop","speed-preset":"ultrafast",'
            '"tune":"zerolatency"}'
        ),
    ) -> None:
        super().__init__()
        self._gstreamer_path = gstreamer_path
        self._gstreamer_inspect_path = gstreamer_inspect_path
        self._rtp_host = rtp_host
        self._rtp_port = int(rtp_port)
        self._width = int(width)
        self._height = int(height)
        self._fps = float(fps)
        self._bitrate = int(bitrate)
        self._input_format = normalize_input_format(input_format)
        self._jpeg_parser = _validate_element_name(jpeg_parser, "gstreamer_jpeg_parser")
        self._jpeg_caps = self._validate_caps(
            jpeg_caps, "image/jpeg", "gstreamer_jpeg_caps"
        )
        self._jpeg_decoder = _validate_element_name(jpeg_decoder, "gstreamer_jpeg_decoder")
        self._video_converter = _validate_element_name(
            video_converter, "gstreamer_video_converter"
        )
        self._video_scaler = _validate_element_name(
            video_scaler, "gstreamer_video_scaler"
        )
        self._h264_encoder = _validate_element_name(
            h264_encoder, "gstreamer_h264_encoder"
        )
        self._raw_caps = self._validate_caps(
            raw_caps, "video/x-raw", "gstreamer_raw_caps"
        )
        self._decoder_properties = _parse_properties(
            decoder_properties, "gstreamer_decoder_properties_json"
        )
        self._converter_properties = _parse_properties(
            converter_properties, "gstreamer_converter_properties_json"
        )
        self._encoder_properties = _parse_properties(
            encoder_properties, "gstreamer_encoder_properties_json"
        )

    def validate_runtime(self) -> None:
        try:
            launch_result = subprocess.run(
                [self._gstreamer_path, "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"GStreamer launcher preflight failed: {exc}") from exc
        if launch_result.returncode != 0:
            detail = launch_result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                "GStreamer launcher preflight failed"
                + (f": {detail}" if detail else "")
            )

        elements = ["fdsrc"]
        if self._input_format == "jpeg":
            elements.extend([self._jpeg_parser, self._jpeg_decoder])
        else:
            elements.append("rawvideoparse")
        elements.extend(
            [
                self._video_converter,
                self._video_scaler,
                self._h264_encoder,
                "h264parse",
                "rtph264pay",
                "udpsink",
            ]
        )
        inspected: Dict[str, str] = {}
        for element in dict.fromkeys(elements):
            try:
                result = subprocess.run(
                    [self._gstreamer_inspect_path, element],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"GStreamer preflight failed for {element}: {exc}") from exc
            if result.returncode != 0:
                detail = result.stdout.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"GStreamer element {element!r} is unavailable"
                    + (f": {detail}" if detail else "")
                )
            inspected[element] = result.stdout.decode("utf-8", errors="replace")

        property_sets = (
            (self._jpeg_decoder, self._decoder_properties)
            if self._input_format == "jpeg"
            else (None, {})
        ), (self._video_converter, self._converter_properties), (
            self._h264_encoder,
            self._encoder_properties,
        )
        for element, properties in property_sets:
            if element is None:
                continue
            output = inspected[element]
            for property_name in properties:
                pattern = re.compile(
                    rf"(?m)^\s+{re.escape(property_name)}\s*:"
                )
                if not pattern.search(output):
                    raise RuntimeError(
                        f"GStreamer element {element!r} has no configured "
                        f"property {property_name!r}"
                    )

    def _build_command(self) -> List[str]:
        command = [
            self._gstreamer_path,
            "-q",
            "fdsrc",
            "fd=0",
            "do-timestamp=true",
        ]
        if self._input_format == "jpeg":
            command.extend(
                [
                    "!",
                    self._expanded_caps(self._jpeg_caps),
                    "!",
                    self._jpeg_parser,
                    "!",
                    self._jpeg_decoder,
                ]
            )
            command.extend(self._property_arguments(self._decoder_properties))
        else:
            raw_format = _RAW_INPUTS[self._input_format][1]
            command.extend(
                [
                    "blocksize=%d"
                    % packed_frame_bytes(
                        self._input_format,
                        self._width,
                        self._height,
                    ),
                    "!",
                    "rawvideoparse",
                    f"format={raw_format}",
                    f"width={self._width}",
                    f"height={self._height}",
                    f"framerate={self._expanded_fps_fraction()}",
                ]
            )
        command.extend(["!", self._video_converter])
        command.extend(self._property_arguments(self._converter_properties))
        command.extend(
            [
                "!",
                self._video_scaler,
                "!",
                self._expanded_caps(self._raw_caps),
                "!",
                self._h264_encoder,
            ]
        )
        command.extend(self._property_arguments(self._encoder_properties))
        command.extend(
            [
                "!",
                "h264parse",
                "config-interval=-1",
                "!",
                "video/x-h264,stream-format=byte-stream,alignment=au",
                "!",
                "rtph264pay",
                "pt=96",
                "mtu=1200",
                "config-interval=-1",
                "!",
                "udpsink",
                f"host={self._rtp_host}",
                f"port={self._rtp_port}",
                "sync=false",
                "async=false",
            ]
        )
        return command

    @staticmethod
    def _validate_caps(value: str, media_type: str, parameter_name: str) -> str:
        if not value.startswith(media_type):
            raise ValueError(f"{parameter_name} must start with {media_type}")
        if "!" in value or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"{parameter_name} contains a forbidden pipeline separator")
        return value

    def _expanded_caps(self, template: str) -> str:
        replacements = {
            "@width": str(self._width),
            "@height": str(self._height),
            "@fps_fraction": self._expanded_fps_fraction(),
            "@fps": _format_number(self._fps),
        }
        caps = template
        for marker, replacement in replacements.items():
            caps = caps.replace(marker, replacement)
        return caps

    def _expanded_fps_fraction(self) -> str:
        fps_fraction = Fraction(str(self._fps)).limit_denominator(1001)
        return f"{fps_fraction.numerator}/{fps_fraction.denominator}"

    def _property_arguments(self, properties: Mapping[str, PropertyValue]) -> List[str]:
        gop = max(1, int(round(self._fps)))
        context: Dict[str, PropertyValue] = {
            "@bitrate": self._bitrate,
            "@bitrate_kbps": max(1, int(round(self._bitrate / 1000.0))),
            "@fps": self._fps,
            "@gop": gop,
            "@width": self._width,
            "@height": self._height,
        }
        arguments = []
        for key, value in properties.items():
            expanded = context.get(value, value) if isinstance(value, str) else value
            if isinstance(expanded, bool):
                rendered = "true" if expanded else "false"
            elif isinstance(expanded, float):
                rendered = _format_number(expanded)
            else:
                rendered = str(expanded)
            arguments.append(f"{key}={rendered}")
        return arguments


def create_rtp_encoder(*, backend: str, **kwargs: Any) -> SubprocessEncoder:
    """Create a backend without auto-detecting a vendor or device model."""

    normalized = backend.strip().lower()
    if normalized == "ffmpeg":
        return FFmpegRtpEncoder(**kwargs)
    if normalized == "gstreamer":
        return GStreamerRtpEncoder(**kwargs)
    raise ValueError("encoder_backend must be one of: ffmpeg, gstreamer")


def create_h264_preview_encoder(*, backend: str, **kwargs: Any) -> FFmpegH264PreviewEncoder:
    """The timestamped ROS preview needs FFmpeg's encoded-packet output."""

    if backend.strip().lower() != "ffmpeg":
        raise ValueError("ROS H264 preview requires the FFmpeg encoder backend")
    return FFmpegH264PreviewEncoder(**kwargs)
