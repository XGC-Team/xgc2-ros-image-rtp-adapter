"""Thin ROS 1 Noetic wrapper around the shared image-to-RTP runtime."""

from __future__ import annotations

import rospy
from sensor_msgs.msg import CompressedImage, Image

from ros_image_rtp_adapter.runtime import ImageRtpAdapterRuntime
from ros_image_rtp_adapter.settings import AdapterSettings, PARAMETER_DEFAULTS


class ImageRtpAdapterROS1Node:
    def __init__(self) -> None:
        values = {
            name: rospy.get_param(f"~{name}", default)
            for name, default in PARAMETER_DEFAULTS.items()
        }
        try:
            self._settings = AdapterSettings.from_mapping(values)
        except ValueError as exc:
            raise rospy.ROSInitException(
                f"invalid image RTP adapter configuration: {exc}"
            )

        self._runtime = ImageRtpAdapterRuntime(
            self._settings,
            log_info=rospy.loginfo,
            log_warning=rospy.logwarn,
            log_error=rospy.logerr,
        )
        self._runtime.start()
        rospy.on_shutdown(self._runtime.stop)

        if self._settings.input_message_type == "compressed":
            self._subscription = rospy.Subscriber(
                self._settings.image_topic,
                CompressedImage,
                self._on_compressed_image,
                queue_size=10,
                buff_size=16 * 1024 * 1024,
            )
        else:
            self._subscription = rospy.Subscriber(
                self._settings.image_topic,
                Image,
                self._on_raw_image,
                queue_size=10,
                buff_size=64 * 1024 * 1024,
            )
        self._pump_timer = rospy.Timer(
            rospy.Duration.from_sec(1.0 / self._settings.fps), self._pump
        )
        self._status_timer = rospy.Timer(rospy.Duration.from_sec(5.0), self._log_status)
        rospy.loginfo(
            "image_rtp_adapter ready: ros=1 topic=%s message=%s source_id=%s "
            "backend=%s input=%s rtp=%s:%d control=%s",
            self._settings.image_topic,
            self._settings.input_message_type,
            self._settings.source_id,
            self._settings.encoder_backend,
            self._settings.encoder_input_format,
            self._settings.rtp_host,
            self._settings.rtp_port,
            self._settings.control_socket,
        )

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

    def _pump(self, _event) -> None:
        self._runtime.pump()

    def _log_status(self, _event) -> None:
        status = self._runtime.status()
        if not status["active"]:
            rospy.loginfo(
                "status idle source_id=%s frames_in=%d encoder_released=%s",
                self._settings.source_id,
                status["frames_in"],
                not status["encoder_running"],
            )
            return
        if not status["encoder_running"]:
            rospy.logerr(
                "encoder backend=%s is not running: %s",
                self._settings.encoder_backend,
                status["encoder_diagnostic"] or "no diagnostic",
            )
            return
        rospy.loginfo(
            "status frames_in=%d frames_out=%d frames_dropped=%d "
            "pending=%d active=%s topic=%s backend=%s",
            status["frames_in"],
            status["frames_out"],
            status["frames_dropped"],
            status["pending"],
            status["active"],
            self._settings.image_topic,
            self._settings.encoder_backend,
        )


def main() -> None:
    rospy.init_node("image_rtp_adapter", anonymous=False)
    ImageRtpAdapterROS1Node()
    rospy.spin()


if __name__ == "__main__":
    main()
