"""ROS 2 node: parameterized CompressedImage → media-edge H264/RTP source."""

from __future__ import annotations

import threading
from typing import Optional

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage

from ros_image_rtp_adapter.control_socket import SourceControlServer, SourceDescription
from ros_image_rtp_adapter.encoder import (
    FFmpegJpegRtpEncoder,
    GStreamerJpegRtpEncoder,
    SubprocessJpegRtpEncoder,
)


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
        self.declare_parameter("encoder_backend", "ffmpeg")
        # FFmpeg backend. `encoder` is retained as the public compatibility
        # name used by existing launch/process definitions.
        self.declare_parameter("encoder", "libx264")
        self.declare_parameter("ffmpeg_path", "ffmpeg")
        self.declare_parameter("ffmpeg_encoder_args_json", "[]")
        self.declare_parameter("ffmpeg_video_filter", "")
        # GStreamer backend. Vendor element factories and properties are data,
        # never device detection branches in the adapter.
        self.declare_parameter("gstreamer_path", "gst-launch-1.0")
        self.declare_parameter("gstreamer_inspect_path", "gst-inspect-1.0")
        self.declare_parameter("gstreamer_jpeg_parser", "jpegparse")
        self.declare_parameter(
            "gstreamer_jpeg_caps", "image/jpeg,framerate=@fps_fraction"
        )
        self.declare_parameter("gstreamer_jpeg_decoder", "jpegdec")
        self.declare_parameter("gstreamer_video_converter", "videoconvert")
        self.declare_parameter("gstreamer_video_scaler", "videoscale")
        self.declare_parameter(
            "gstreamer_raw_caps",
            "video/x-raw,format=I420,width=@width,height=@height,"
            "framerate=@fps_fraction",
        )
        self.declare_parameter("gstreamer_h264_encoder", "x264enc")
        self.declare_parameter("gstreamer_decoder_properties_json", "{}")
        self.declare_parameter("gstreamer_converter_properties_json", "{}")
        self.declare_parameter(
            "gstreamer_encoder_properties_json",
            '{"bitrate":"@bitrate_kbps","byte-stream":true,'
            '"key-int-max":"@gop","speed-preset":"ultrafast",'
            '"tune":"zerolatency"}',
        )
        self.declare_parameter("drop_to_latest", True)
        self.declare_parameter("require_jpeg", True)

        self._image_topic = str(self.get_parameter("image_topic").value)
        self._source_id = str(self.get_parameter("source_id").value)
        self._frame_id = str(self.get_parameter("frame_id").value)
        self._rtp_host = str(self.get_parameter("rtp_host").value)
        self._rtp_port = int(self.get_parameter("rtp_port").value)
        self._control_socket = str(self.get_parameter("control_socket").value)
        self._width = int(self.get_parameter("width").value)
        self._height = int(self.get_parameter("height").value)
        # CLI often passes fps as an integer; accept both int and float.
        self._fps = float(self.get_parameter("fps").value)
        self._bitrate = int(self.get_parameter("bitrate").value)
        self._encoder_backend = str(self.get_parameter("encoder_backend").value).lower()
        self._encoder = str(self.get_parameter("encoder").value)
        self._ffmpeg_path = str(self.get_parameter("ffmpeg_path").value)
        self._ffmpeg_encoder_args_json = str(
            self.get_parameter("ffmpeg_encoder_args_json").value
        )
        self._ffmpeg_video_filter = str(self.get_parameter("ffmpeg_video_filter").value)
        self._gstreamer_path = str(self.get_parameter("gstreamer_path").value)
        self._gstreamer_inspect_path = str(
            self.get_parameter("gstreamer_inspect_path").value
        )
        self._gstreamer_jpeg_parser = str(
            self.get_parameter("gstreamer_jpeg_parser").value
        )
        self._gstreamer_jpeg_caps = str(
            self.get_parameter("gstreamer_jpeg_caps").value
        )
        self._gstreamer_jpeg_decoder = str(
            self.get_parameter("gstreamer_jpeg_decoder").value
        )
        self._gstreamer_video_converter = str(
            self.get_parameter("gstreamer_video_converter").value
        )
        self._gstreamer_video_scaler = str(
            self.get_parameter("gstreamer_video_scaler").value
        )
        self._gstreamer_raw_caps = str(self.get_parameter("gstreamer_raw_caps").value)
        self._gstreamer_h264_encoder = str(
            self.get_parameter("gstreamer_h264_encoder").value
        )
        self._gstreamer_decoder_properties_json = str(
            self.get_parameter("gstreamer_decoder_properties_json").value
        )
        self._gstreamer_converter_properties_json = str(
            self.get_parameter("gstreamer_converter_properties_json").value
        )
        self._gstreamer_encoder_properties_json = str(
            self.get_parameter("gstreamer_encoder_properties_json").value
        )
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
        if self._width < 1 or self._height < 1:
            raise RuntimeError("width and height must be positive")
        if self._fps <= 0 or self._bitrate < 1:
            raise RuntimeError("fps and bitrate must be positive")
        if self._encoder_backend not in {"ffmpeg", "gstreamer"}:
            raise RuntimeError("encoder_backend must be one of: ffmpeg, gstreamer")

        self._latest_jpeg: Optional[bytes] = None
        self._lock = threading.Lock()
        self._frames_in = 0
        self._frames_out = 0
        self._active = True

        self._encoder_worker = self._create_encoder()
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
            "image_rtp_adapter ready: topic=%s source_id=%s backend=%s "
            "rtp=%s:%d control=%s"
            % (
                self._image_topic,
                self._source_id,
                self._encoder_backend,
                self._rtp_host,
                self._rtp_port,
                self._control_socket,
            )
        )

    def _create_encoder(self) -> SubprocessJpegRtpEncoder:
        common = {
            "rtp_host": self._rtp_host,
            "rtp_port": self._rtp_port,
            "width": self._width,
            "height": self._height,
            "fps": self._fps,
            "bitrate": self._bitrate,
        }
        if self._encoder_backend == "ffmpeg":
            return FFmpegJpegRtpEncoder(
                **common,
                ffmpeg_path=self._ffmpeg_path,
                encoder=self._encoder,
                encoder_args=self._ffmpeg_encoder_args_json,
                video_filter=self._ffmpeg_video_filter,
            )
        return GStreamerJpegRtpEncoder(
            **common,
            gstreamer_path=self._gstreamer_path,
            gstreamer_inspect_path=self._gstreamer_inspect_path,
            jpeg_parser=self._gstreamer_jpeg_parser,
            jpeg_caps=self._gstreamer_jpeg_caps,
            jpeg_decoder=self._gstreamer_jpeg_decoder,
            video_converter=self._gstreamer_video_converter,
            video_scaler=self._gstreamer_video_scaler,
            raw_caps=self._gstreamer_raw_caps,
            h264_encoder=self._gstreamer_h264_encoder,
            decoder_properties=self._gstreamer_decoder_properties_json,
            converter_properties=self._gstreamer_converter_properties_json,
            encoder_properties=self._gstreamer_encoder_properties_json,
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
        if not self._encoder_worker.running:
            self.get_logger().error(
                "encoder backend=%s is not running: %s"
                % (self._encoder_backend, self._encoder_worker.diagnostic or "no diagnostic")
            )
            return
        self.get_logger().info(
            "status frames_in=%d frames_out=%d active=%s topic=%s backend=%s"
            % (
                self._frames_in,
                self._frames_out,
                self._active,
                self._image_topic,
                self._encoder_backend,
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
