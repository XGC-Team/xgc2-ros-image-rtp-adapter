"""Publish synthetic JPEG CompressedImage frames for CI / local smoke."""

from __future__ import annotations

import argparse
import io
import math
import time

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage


def _rgb_motion_frame(width: int, height: int, phase: float, frame_index: int):
    """Return HxWx3 uint8 numpy array with strong per-frame motion."""
    import numpy as np

    yy, xx = np.mgrid[0:height, 0:width]
    # Scrolling rainbow-ish background (high temporal change).
    wave = (xx * 3 + yy + int(phase * 80)) % 256
    r = ((wave + int(phase * 40)) % 256).astype(np.uint8)
    g = ((wave * 2 + 80) % 256).astype(np.uint8)
    b = ((255 - wave + int(phase * 25)) % 256).astype(np.uint8)
    rgb = np.stack([r, g, b], axis=-1)

    # Horizontal sweep bar.
    bar_y = int((0.15 + 0.7 * (0.5 + 0.5 * math.sin(phase * 1.7))) * height)
    y0 = max(0, bar_y - 6)
    y1 = min(height, bar_y + 6)
    rgb[y0:y1, :, :] = (255, 200, 0)

    # Bouncing ball.
    cx = int((0.5 + 0.4 * math.sin(phase * 1.3)) * width)
    cy = int((0.5 + 0.35 * math.cos(phase * 0.9)) * height)
    rad = max(12, min(width, height) // 10)
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= rad * rad
    rgb[mask] = (0, 220, 255)
    rim = ((xx - cx) ** 2 + (yy - cy) ** 2 <= (rad + 2) ** 2) & ~mask
    rgb[rim] = (255, 255, 255)

    # Top HUD bar + frame counter stripe (unique every frame).
    rgb[0:36, :, :] = 0
    # Encode frame_index as a moving white block so even without text it ticks.
    mark = int(frame_index * 7) % max(1, width - 20)
    rgb[8:28, mark : mark + 18, :] = 255
    return rgb


def _jpeg_from_rgb_pil(rgb) -> bytes:
    from PIL import Image

    image = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


def _jpeg_from_rgb_ffmpeg(rgb, width: int, height: int, ffmpeg_path: str) -> bytes:
    """Encode one RGB frame to JPEG via ffmpeg (no Pillow dependency)."""
    import subprocess

    cmd = [
        ffmpeg_path,
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        "%dx%d" % (width, height),
        "-i",
        "pipe:0",
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    proc = subprocess.run(
        cmd,
        input=rgb.tobytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(
            "ffmpeg jpeg encode failed rc=%s stderr=%s"
            % (proc.returncode, proc.stderr[:200])
        )
    return proc.stdout


def _make_jpeg(
    width: int,
    height: int,
    phase: float,
    frame_index: int,
    *,
    ffmpeg_path: str = "ffmpeg",
) -> bytes:
    """Synthetic moving scene — not a still image.

    Deliberately high inter-frame change so soft H264 and browser preview
    clearly show motion (scroll bars, bouncing ball, ticking counter).
    Prefers Pillow; falls back to ffmpeg mjpeg when python3-pil is absent.
    """
    rgb = _rgb_motion_frame(width, height, phase, frame_index)
    try:
        return _jpeg_from_rgb_pil(rgb)
    except ImportError:
        return _jpeg_from_rgb_ffmpeg(rgb, width, height, ffmpeg_path)

class TestJpegPublisher(Node):
    def __init__(
        self,
        topic: str,
        width: int,
        height: int,
        fps: float,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        super().__init__("publish_test_jpeg")
        self._pub = self.create_publisher(CompressedImage, topic, 10)
        self._width = width
        self._height = height
        self._fps = fps
        self._ffmpeg_path = ffmpeg_path
        self._t0 = time.monotonic()
        self._frame_index = 0
        self._timer = self.create_timer(1.0 / max(fps, 1.0), self._tick)
        self.get_logger().info(
            "publishing moving test JPEG on %s @ %.1f Hz (animated, not still)"
            % (topic, fps)
        )

    def _tick(self) -> None:
        phase = time.monotonic() - self._t0
        self._frame_index += 1
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "camera_optical"
        msg.format = "jpeg"
        msg.data = list(
            _make_jpeg(
                self._width,
                self._height,
                phase,
                self._frame_index,
                ffmpeg_path=self._ffmpeg_path,
            )
        )
        self._pub.publish(msg)


def main(args=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default="/camera/image_raw/compressed")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    known, ros_args = parser.parse_known_args()

    rclpy.init(args=ros_args)
    node = TestJpegPublisher(
        known.topic,
        known.width,
        known.height,
        known.fps,
        ffmpeg_path=known.ffmpeg_path,
    )
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
