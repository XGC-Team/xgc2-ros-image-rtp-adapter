"""One parameter contract shared by the ROS 1 and ROS 2 wrappers."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Any, Dict, Mapping

from ros_image_rtp_adapter.frames import normalize_raw_encoding


PARAMETER_DEFAULTS: Dict[str, Any] = {
    "image_topic": "/camera/image_raw/compressed",
    "input_message_type": "compressed",
    "raw_encoding": "bgr8",
    "source_id": "camera",
    "frame_id": "camera_optical",
    "rtp_host": "127.0.0.1",
    "rtp_port": 5004,
    "control_socket": "/tmp/xgc2-image-rtp-adapter.sock",
    "width": 1280,
    "height": 720,
    "fps": 15.0,
    "bitrate": 2_500_000,
    "encoder_backend": "ffmpeg",
    "encoder": "libx264",
    "ffmpeg_path": "ffmpeg",
    "ffmpeg_encoder_args_json": "[]",
    "ffmpeg_video_filter": "",
    "gstreamer_path": "gst-launch-1.0",
    "gstreamer_inspect_path": "gst-inspect-1.0",
    "gstreamer_jpeg_parser": "jpegparse",
    "gstreamer_jpeg_caps": "image/jpeg,framerate=@fps_fraction",
    "gstreamer_jpeg_decoder": "jpegdec",
    "gstreamer_video_converter": "videoconvert",
    "gstreamer_video_scaler": "videoscale",
    "gstreamer_raw_caps": (
        "video/x-raw,format=I420,width=@width,height=@height,"
        "framerate=@fps_fraction"
    ),
    "gstreamer_h264_encoder": "x264enc",
    "gstreamer_decoder_properties_json": "{}",
    "gstreamer_converter_properties_json": "{}",
    "gstreamer_encoder_properties_json": (
        '{"bitrate":"@bitrate_kbps","byte-stream":true,'
        '"key-int-max":"@gop","speed-preset":"ultrafast",'
        '"tune":"zerolatency"}'
    ),
    "drop_to_latest": True,
    "require_jpeg": True,
}

_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class AdapterSettings:
    image_topic: str
    input_message_type: str
    raw_encoding: str
    source_id: str
    frame_id: str
    rtp_host: str
    rtp_port: int
    control_socket: str
    width: int
    height: int
    fps: float
    bitrate: int
    encoder_backend: str
    encoder: str
    ffmpeg_path: str
    ffmpeg_encoder_args_json: str
    ffmpeg_video_filter: str
    gstreamer_path: str
    gstreamer_inspect_path: str
    gstreamer_jpeg_parser: str
    gstreamer_jpeg_caps: str
    gstreamer_jpeg_decoder: str
    gstreamer_video_converter: str
    gstreamer_video_scaler: str
    gstreamer_raw_caps: str
    gstreamer_h264_encoder: str
    gstreamer_decoder_properties_json: str
    gstreamer_converter_properties_json: str
    gstreamer_encoder_properties_json: str
    drop_to_latest: bool
    require_jpeg: bool

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "AdapterSettings":
        merged = dict(PARAMETER_DEFAULTS)
        merged.update(values)
        settings = cls(
            image_topic=str(merged["image_topic"]).strip(),
            input_message_type=str(merged["input_message_type"]).strip().lower(),
            raw_encoding=str(merged["raw_encoding"]).strip().lower(),
            source_id=str(merged["source_id"]).strip(),
            frame_id=str(merged["frame_id"]).strip(),
            rtp_host=str(merged["rtp_host"]).strip(),
            rtp_port=int(merged["rtp_port"]),
            control_socket=str(merged["control_socket"]).strip(),
            width=int(merged["width"]),
            height=int(merged["height"]),
            fps=float(merged["fps"]),
            bitrate=int(merged["bitrate"]),
            encoder_backend=str(merged["encoder_backend"]).strip().lower(),
            encoder=str(merged["encoder"]).strip(),
            ffmpeg_path=str(merged["ffmpeg_path"]).strip(),
            ffmpeg_encoder_args_json=str(merged["ffmpeg_encoder_args_json"]),
            ffmpeg_video_filter=str(merged["ffmpeg_video_filter"]),
            gstreamer_path=str(merged["gstreamer_path"]).strip(),
            gstreamer_inspect_path=str(merged["gstreamer_inspect_path"]).strip(),
            gstreamer_jpeg_parser=str(merged["gstreamer_jpeg_parser"]).strip(),
            gstreamer_jpeg_caps=str(merged["gstreamer_jpeg_caps"]),
            gstreamer_jpeg_decoder=str(merged["gstreamer_jpeg_decoder"]).strip(),
            gstreamer_video_converter=str(
                merged["gstreamer_video_converter"]
            ).strip(),
            gstreamer_video_scaler=str(merged["gstreamer_video_scaler"]).strip(),
            gstreamer_raw_caps=str(merged["gstreamer_raw_caps"]),
            gstreamer_h264_encoder=str(merged["gstreamer_h264_encoder"]).strip(),
            gstreamer_decoder_properties_json=str(
                merged["gstreamer_decoder_properties_json"]
            ),
            gstreamer_converter_properties_json=str(
                merged["gstreamer_converter_properties_json"]
            ),
            gstreamer_encoder_properties_json=str(
                merged["gstreamer_encoder_properties_json"]
            ),
            drop_to_latest=bool(merged["drop_to_latest"]),
            require_jpeg=bool(merged["require_jpeg"]),
        )
        settings.validate()
        return settings

    @property
    def encoder_input_format(self) -> str:
        return "jpeg" if self.input_message_type == "compressed" else self.raw_encoding

    def validate(self) -> None:
        if not self.image_topic:
            raise ValueError("image_topic must be a non-empty ROS topic name")
        if self.input_message_type not in {"compressed", "raw"}:
            raise ValueError("input_message_type must be one of: compressed, raw")
        normalize_raw_encoding(self.raw_encoding)
        if not _STABLE_ID.fullmatch(self.source_id):
            raise ValueError("source_id must be a stable identifier")
        if not self.frame_id:
            raise ValueError("frame_id must be non-empty")
        if self.rtp_host != "localhost":
            try:
                if not ipaddress.ip_address(self.rtp_host).is_loopback:
                    raise ValueError("rtp_host must be loopback")
            except ValueError as exc:
                raise ValueError("rtp_host must be loopback") from exc
        if self.rtp_port < 1 or self.rtp_port > 65_535:
            raise ValueError("rtp_port must be in 1..65535")
        if not self.control_socket.startswith("/"):
            raise ValueError("control_socket must be an absolute Unix socket path")
        if self.width < 16 or self.height < 16:
            raise ValueError("width and height must be at least 16")
        if self.fps <= 0 or self.fps > 240:
            raise ValueError("fps must be in (0, 240]")
        if self.bitrate < 1:
            raise ValueError("bitrate must be positive")
        if self.encoder_backend not in {"ffmpeg", "gstreamer"}:
            raise ValueError("encoder_backend must be one of: ffmpeg, gstreamer")

    def encoder_kwargs(self) -> Dict[str, Any]:
        common = {
            "rtp_host": self.rtp_host,
            "rtp_port": self.rtp_port,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "bitrate": self.bitrate,
            "input_format": self.encoder_input_format,
        }
        if self.encoder_backend == "ffmpeg":
            common.update(
                {
                    "ffmpeg_path": self.ffmpeg_path,
                    "encoder": self.encoder,
                    "encoder_args": self.ffmpeg_encoder_args_json,
                    "video_filter": self.ffmpeg_video_filter,
                }
            )
        else:
            common.update(
                {
                    "gstreamer_path": self.gstreamer_path,
                    "gstreamer_inspect_path": self.gstreamer_inspect_path,
                    "jpeg_parser": self.gstreamer_jpeg_parser,
                    "jpeg_caps": self.gstreamer_jpeg_caps,
                    "jpeg_decoder": self.gstreamer_jpeg_decoder,
                    "video_converter": self.gstreamer_video_converter,
                    "video_scaler": self.gstreamer_video_scaler,
                    "raw_caps": self.gstreamer_raw_caps,
                    "h264_encoder": self.gstreamer_h264_encoder,
                    "decoder_properties": self.gstreamer_decoder_properties_json,
                    "converter_properties": self.gstreamer_converter_properties_json,
                    "encoder_properties": self.gstreamer_encoder_properties_json,
                }
            )
        return common
