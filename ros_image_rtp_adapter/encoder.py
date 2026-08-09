"""JPEG bytes → H264/RTP via ffmpeg (soft-encode path, product default)."""

from __future__ import annotations

import signal
import subprocess
import threading
from typing import List, Optional


class FFmpegJpegRtpEncoder:
    """
    Feed complete JPEG frames on stdin; ffmpeg re-encodes to H264 and packetizes
    RTP to a loopback destination for xgc2-media-edge.
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
        encoder: str = "libx264",
    ) -> None:
        self._ffmpeg_path = ffmpeg_path
        self._rtp_host = rtp_host
        self._rtp_port = int(rtp_port)
        self._width = int(width)
        self._height = int(height)
        self._fps = float(fps)
        self._bitrate = int(bitrate)
        self._encoder = encoder
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._proc is not None:
                return
            cmd = self._build_command()
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            # Drain stderr so the pipe cannot block ffmpeg.
            threading.Thread(target=self._drain_stderr, daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass

    def write_jpeg(self, jpeg: bytes) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(jpeg)
                proc.stdin.flush()
            except BrokenPipeError:
                self._restart_locked()

    def request_keyframe(self) -> None:
        # ffmpeg's stdin-driven soft encoder has no safe per-frame force-IDR
        # control. Restarting ffmpeg here changes the RTP producer clock/SSRC
        # while a WebRTC session is live and can trap the browser in a PLI ->
        # restart loop. The command below emits an IDR with repeated SPS/PPS at
        # least once per second, so a request is satisfied by the next bounded
        # periodic keyframe without breaking RTP continuity.
        return

    def _restart_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass
        cmd = self._build_command()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _build_command(self) -> List[str]:
        # image2pipe + mjpeg demux accepts concatenated JPEG frames on stdin.
        gop = max(1, int(round(self._fps)))
        command = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-framerate",
            str(self._fps),
            "-i",
            "pipe:0",
            "-an",
            "-vf",
            f"scale={self._width}:{self._height}:force_original_aspect_ratio=decrease,"
            f"pad={self._width}:{self._height}:(ow-iw)/2:(oh-ih)/2",
            "-c:v",
            self._encoder,
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            "-bf",
            "0",
            "-b:v",
            str(self._bitrate),
            "-maxrate",
            str(self._bitrate),
            "-bufsize",
            str(self._bitrate * 2),
        ]
        if self._encoder == "libx264":
            command.extend([
                "-x264-params",
                "repeat-headers=1:scenecut=0",
            ])
        command.extend([
            "-f",
            "rtp",
            "-payload_type",
            "96",
            f"rtp://{self._rtp_host}:{self._rtp_port}?pkt_size=1200",
        ])
        return command

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for _line in iter(proc.stderr.readline, b""):
                if self._proc is None:
                    break
        except Exception:
            pass
