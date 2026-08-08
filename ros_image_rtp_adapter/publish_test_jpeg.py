"""Publish synthetic JPEG CompressedImage frames for CI / local smoke."""

from __future__ import annotations

import argparse
import io
import math
import time

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage


def _make_jpeg(width: int, height: int, phase: float) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Pillow is required for publish_test_jpeg (python3-pil). %s" % exc
        ) from exc

    image = Image.new("RGB", (width, height), (20, 24, 40))
    draw = ImageDraw.Draw(image)
    cx = int((0.5 + 0.35 * math.sin(phase)) * width)
    cy = int((0.5 + 0.35 * math.cos(phase * 0.7)) * height)
    r = min(width, height) // 8
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 180, 255))
    draw.rectangle((10, 10, width - 10, 40), fill=(0, 0, 0))
    draw.text((16, 14), "xgc2 ros_image_rtp_adapter test", fill=(255, 255, 255))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


class TestJpegPublisher(Node):
    def __init__(self, topic: str, width: int, height: int, fps: float) -> None:
        super().__init__("publish_test_jpeg")
        self._pub = self.create_publisher(CompressedImage, topic, 10)
        self._width = width
        self._height = height
        self._fps = fps
        self._t0 = time.monotonic()
        self._timer = self.create_timer(1.0 / max(fps, 1.0), self._tick)
        self.get_logger().info("publishing test JPEG on %s @ %.1f Hz" % (topic, fps))

    def _tick(self) -> None:
        phase = time.monotonic() - self._t0
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "camera_optical"
        msg.format = "jpeg"
        msg.data = list(_make_jpeg(self._width, self._height, phase))
        self._pub.publish(msg)


def main(args=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default="/camera/image_raw/compressed")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--fps", type=float, default=10.0)
    known, ros_args = parser.parse_known_args()

    rclpy.init(args=ros_args)
    node = TestJpegPublisher(known.topic, known.width, known.height, known.fps)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
