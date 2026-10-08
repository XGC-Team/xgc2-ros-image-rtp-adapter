"""Thin ROS 2 wrapper around the shared image-to-RTP runtime."""

from __future__ import annotations

import rclpy
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from sensor_msgs.msg import CompressedImage, Image

from ros_image_rtp_adapter.runtime import ImageRtpAdapterRuntime, finish_shutdown, shutdown_signal_owner
from ros_image_rtp_adapter.settings import AdapterSettings, PARAMETER_DEFAULTS


class ImageRtpAdapterNode(Node):
    def __init__(self) -> None:
        super().__init__("image_rtp_adapter")
        self._destroyed = False
        for name, default in PARAMETER_DEFAULTS.items():
            self.declare_parameter(name, default)

        values = {
            name: self.get_parameter(name).value for name in PARAMETER_DEFAULTS
        }
        try:
            self._settings = AdapterSettings.from_mapping(values)
        except ValueError as exc:
            raise RuntimeError(f"invalid image RTP adapter configuration: {exc}") from exc
        if self._settings.video_topic:
            # Fail closed instead of silently ignoring a configured preview.
            raise RuntimeError("the ROS H264 preview output is implemented for ROS 1 only")

        self._runtime = ImageRtpAdapterRuntime(
            self._settings,
            log_info=self.get_logger().info,
            log_warning=self.get_logger().warning,
            log_error=self.get_logger().error,
        )
        try:
            self._initialize_runtime()
        except BaseException:
            # main has no node reference until __init__ returns. This frame
            # owns self/context through every failed native cleanup attempt.
            finish_shutdown(self.destroy_node, lambda message: self.get_logger().error(message))
            raise

    def _initialize_runtime(self) -> None:
        self._runtime.start()

        if self._settings.input_message_type == "compressed":
            self._subscription = self.create_subscription(
                CompressedImage,
                self._settings.image_topic,
                self._on_compressed_image,
                1,
            )
        else:
            self._subscription = self.create_subscription(
                Image,
                self._settings.image_topic,
                self._on_raw_image,
                1,
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
        if self._destroyed:
            return True
        try:
            self._runtime.stop()
        except Exception as exc:
            self.get_logger().error(f"stop image RTP adapter runtime: {exc}")
            return False
        result = super().destroy_node()
        if result:
            self._destroyed = True
        return result

    def _on_compressed_image(self, message: CompressedImage) -> None:
        self._runtime.submit_compressed(message.data, message.format,
            source_stamp_ns=message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec,
            frame_id=message.header.frame_id)

    def _on_raw_image(self, message: Image) -> None:
        self._runtime.submit_raw(
            message.data,
            width=message.width,
            height=message.height,
            step=message.step,
            encoding=message.encoding,
            source_stamp_ns=message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec,
            frame_id=message.header.frame_id,
        )

    def _log_status(self) -> None:
        logger = self.get_logger()
        for level, message in self._runtime.status_report():
            (logger.error if level == "error" else logger.info)("status " + message)


def main(args=None) -> None:
    # ROS's default signal handler shuts the context down before user cleanup.
    # This process owner keeps it alive through native stop/reap and retries.
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    with shutdown_signal_owner() as requested:
        node = None
        try:
            node = ImageRtpAdapterNode()
            while rclpy.ok() and not requested.is_set():
                rclpy.spin_once(node, timeout_sec=0.1)
        except KeyboardInterrupt:
            pass
        finally:
            if node is not None:
                finish_shutdown(node.destroy_node, node.get_logger().error)
            rclpy.shutdown()


if __name__ == "__main__":
    main()
