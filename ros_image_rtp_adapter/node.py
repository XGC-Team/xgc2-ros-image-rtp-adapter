"""Thin ROS 2 wrapper around the shared image-to-RTP runtime."""

from __future__ import annotations

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage, Image

from ros_image_rtp_adapter.runtime import ImageRtpAdapterRuntime
from ros_image_rtp_adapter.settings import AdapterSettings, PARAMETER_DEFAULTS


class ImageRtpAdapterNode(Node):
    def __init__(self) -> None:
        super().__init__("image_rtp_adapter")
        for name, default in PARAMETER_DEFAULTS.items():
            self.declare_parameter(name, default)

        values = {
            name: self.get_parameter(name).value for name in PARAMETER_DEFAULTS
        }
        try:
            self._settings = AdapterSettings.from_mapping(values)
        except ValueError as exc:
            raise RuntimeError(f"invalid image RTP adapter configuration: {exc}") from exc

        self._runtime = ImageRtpAdapterRuntime(
            self._settings,
            log_info=self.get_logger().info,
            log_warning=self.get_logger().warning,
            log_error=self.get_logger().error,
        )
        self._runtime.start()

        if self._settings.input_message_type == "compressed":
            self._subscription = self.create_subscription(
                CompressedImage,
                self._settings.image_topic,
                self._on_compressed_image,
                10,
            )
        else:
            self._subscription = self.create_subscription(
                Image,
                self._settings.image_topic,
                self._on_raw_image,
                10,
            )
        self._status_timer = self.create_timer(5.0, self._log_status)

        self.get_logger().info(
            "image_rtp_adapter ready: ros=2 topic=%s message=%s source_id=%s "
            "backend=%s input=%s rtp=%s:%d control=%s"
            % (
                self._settings.image_topic,
                self._settings.input_message_type,
                self._settings.source_id,
                self._settings.encoder_backend,
                self._settings.encoder_input_format,
                self._settings.rtp_host,
                self._settings.rtp_port,
                self._settings.control_socket,
            )
        )

    def destroy_node(self) -> bool:
        try:
            self._runtime.stop()
        except Exception as exc:
            self.get_logger().error(f"stop image RTP adapter runtime: {exc}")
        return super().destroy_node()

    def _on_compressed_image(self, message: CompressedImage) -> None:
        self._runtime.submit_compressed(bytes(message.data), message.format)

    def _on_raw_image(self, message: Image) -> None:
        self._runtime.submit_raw(
            bytes(message.data),
            width=message.width,
            height=message.height,
            step=message.step,
            encoding=message.encoding,
        )

    def _log_status(self) -> None:
        status = self._runtime.status()
        if not status["active"]:
            self.get_logger().info(
                "status idle source_id=%s frames_in=%d encoder_released=%s"
                % (
                    self._settings.source_id,
                    status["frames_in"],
                    not status["encoder_running"],
                )
            )
            return
        if not status["encoder_running"]:
            self.get_logger().error(
                "encoder backend=%s is not running: %s"
                % (
                    self._settings.encoder_backend,
                    status["encoder_diagnostic"] or "no diagnostic",
                )
            )
            return
        self.get_logger().info(
            "status frames_in=%d frames_out=%d frames_dropped=%d "
            "pending=%d active=%s topic=%s backend=%s"
            % (
                status["frames_in"],
                status["frames_out"],
                status["frames_dropped"],
                status["pending"],
                status["active"],
                self._settings.image_topic,
                self._settings.encoder_backend,
            )
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
