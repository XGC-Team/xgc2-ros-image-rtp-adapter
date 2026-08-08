"""ROS 2 node: parameterized CompressedImage → media-edge H264/RTP source."""

from __future__ import annotations

import threading
from typing import Optional

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage

from ros_image_rtp_adapter.control_socket import SourceControlServer, SourceDescription
from ros_image_rtp_adapter.encoder import FFmpegJpegRtpEncoder


class ImageRtpAdapterNode(Node):
    def __init__(self) -> None:
        super().__init__("image_rtp_adapter")

        self.declare_parameter("image_topic", "/camera/image_raw/compressed")
        self.declare_parameter("source_id", "camera")
        self.declare_parameter("frame_id", "camera_optical")
        self.declare_parameter("rtp_host", "127.0.0.1")
        self.declare_parameter("rtp_port", 5004)
        self.declare_parameter("control_socket", "/tmp/xgc2-image-rtp-adapter.sock")
        self.declare_parameter("width", 1280)
        self.declare_parameter("height", 720)
        self.declare_parameter("fps", 15.0)
        self.declare_parameter("bitrate", 2_500_000)
        self.declare_parameter("encoder", "libx264")
        self.declare_parameter("ffmpeg_path", "ffmpeg")
        self.declare_parameter("drop_to_latest", True)
        self.declare_parameter("require_jpeg", True)

        self._image_topic = self.get_parameter("image_topic").get_parameter_value().string_value
        self._source_id = self.get_parameter("source_id").get_parameter_value().string_value
        self._frame_id = self.get_parameter("frame_id").get_parameter_value().string_value
        self._rtp_host = self.get_parameter("rtp_host").get_parameter_value().string_value
        self._rtp_port = int(self.get_parameter("rtp_port").value)
        self._control_socket = self.get_parameter("control_socket").get_parameter_value().string_value
        self._width = int(self.get_parameter("width").value)
        self._height = int(self.get_parameter("height").value)
        self._fps = float(self.get_parameter("fps").value)
        self._bitrate = int(self.get_parameter("bitrate").value)
        self._encoder = self.get_parameter("encoder").get_parameter_value().string_value
        self._ffmpeg_path = self.get_parameter("ffmpeg_path").get_parameter_value().string_value
        self._drop_to_latest = bool(self.get_parameter("drop_to_latest").value)
        self._require_jpeg = bool(self.get_parameter("require_jpeg").value)

        if not self._image_topic:
            raise RuntimeError("image_topic parameter must be a non-empty ROS topic name")
        if not self._source_id:
            raise RuntimeError("source_id parameter must be non-empty")
        if self._rtp_port < 1 or self._rtp_port > 65535:
            raise RuntimeError("rtp_port must be in 1..65535")
        if not self._control_socket.startswith("/"):
            raise RuntimeError("control_socket must be an absolute Unix socket path")

        self._latest_jpeg: Optional[bytes] = None
        self._lock = threading.Lock()
        self._frames_in = 0
        self._frames_out = 0
        self._active = True

        self._encoder_worker = FFmpegJpegRtpEncoder(
            ffmpeg_path=self._ffmpeg_path,
            rtp_host=self._rtp_host,
            rtp_port=self._rtp_port,
            width=self._width,
            height=self._height,
            fps=self._fps,
            bitrate=self._bitrate,
            encoder=self._encoder,
        )
        self._encoder_worker.start()

        description = SourceDescription(
            source_id=self._source_id,
            rtp_host=self._rtp_host,
            rtp_port=self._rtp_port,
            width=self._width,
            height=self._height,
            fps=self._fps,
            frame_id=self._frame_id,
        )
        self._control = SourceControlServer(
            self._control_socket,
            description,
            on_set_active=self._on_set_active,
            on_request_keyframe=self._encoder_worker.request_keyframe,
            on_snapshot=self._snapshot_jpeg,
        )
        self._control.start()

        self._sub = self.create_subscription(
            CompressedImage,
            self._image_topic,
            self._on_image,
            10,
        )
        self._timer = self.create_timer(1.0 / max(self._fps, 1.0), self._pump)
        self._status_timer = self.create_timer(5.0, self._log_status)

        self.get_logger().info(
            "image_rtp_adapter ready: topic=%s source_id=%s rtp=%s:%d control=%s"
            % (self._image_topic, self._source_id, self._rtp_host, self._rtp_port, self._control_socket)
        )

    def destroy_node(self) -> bool:
        try:
            self._control.stop()
        except Exception:
            pass
        try:
            self._encoder_worker.stop()
        except Exception:
            pass
        return super().destroy_node()

    def _on_set_active(self, active: bool) -> None:
        self._active = active
        self.get_logger().info("set-active -> %s" % active)

    def _snapshot_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_jpeg

    def _on_image(self, msg: CompressedImage) -> None:
        fmt = (msg.format or "").lower()
        if self._require_jpeg and "jpeg" not in fmt and "jpg" not in fmt:
            self.get_logger().warning(
                "skipping non-JPEG CompressedImage format=%r (require_jpeg=true)" % msg.format,
                throttle_duration_sec=5.0,
            )
            return
        data = bytes(msg.data)
        if not data:
            return
        with self._lock:
            self._latest_jpeg = data
            self._frames_in += 1
            if not self._drop_to_latest and self._active:
                # Immediate path still uses latest-only buffer for encoder pacing.
                pass

    def _pump(self) -> None:
        if not self._active or not self._control.active:
            return
        with self._lock:
            jpeg = self._latest_jpeg
            if jpeg is None:
                return
            if self._drop_to_latest:
                # Keep the same buffer until a newer frame arrives; encoder may
                # re-send last JPEG at target fps so RTP does not stall.
                pass
        self._encoder_worker.write_jpeg(jpeg)
        self._frames_out += 1

    def _log_status(self) -> None:
        self.get_logger().info(
            "status frames_in=%d frames_out=%d active=%s topic=%s"
            % (self._frames_in, self._frames_out, self._active, self._image_topic)
        )


def main(args=None) -> None:
    rclpy.init(args=args)
    node = ImageRtpAdapterNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
