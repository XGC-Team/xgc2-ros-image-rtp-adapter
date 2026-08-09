"""Launch the parameterized image → media-edge RTP adapter."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "image_topic",
                default_value="/camera/image_raw/compressed",
                description="sensor_msgs/Image or CompressedImage topic.",
            ),
            DeclareLaunchArgument(
                "input_message_type",
                default_value="compressed",
                description="Explicit ROS message contract: compressed or raw.",
            ),
            DeclareLaunchArgument(
                "raw_encoding",
                default_value="bgr8",
                description="Packed encoding required when input_message_type=raw.",
            ),
            DeclareLaunchArgument("source_id", default_value="camera"),
            DeclareLaunchArgument("frame_id", default_value="camera_optical"),
            DeclareLaunchArgument("rtp_host", default_value="127.0.0.1"),
            DeclareLaunchArgument("rtp_port", default_value="5004"),
            DeclareLaunchArgument(
                "control_socket",
                default_value="/tmp/xgc2-image-rtp-adapter.sock",
            ),
            DeclareLaunchArgument("width", default_value="1280"),
            DeclareLaunchArgument("height", default_value="720"),
            DeclareLaunchArgument("fps", default_value="15.0"),
            DeclareLaunchArgument("bitrate", default_value="2500000"),
            DeclareLaunchArgument(
                "encoder_backend",
                default_value="ffmpeg",
                description="Encoder backend: ffmpeg or gstreamer.",
            ),
            DeclareLaunchArgument("encoder", default_value="libx264"),
            DeclareLaunchArgument("ffmpeg_path", default_value="ffmpeg"),
            DeclareLaunchArgument("ffmpeg_encoder_args_json", default_value="[]"),
            DeclareLaunchArgument("ffmpeg_video_filter", default_value=""),
            DeclareLaunchArgument("gstreamer_path", default_value="gst-launch-1.0"),
            DeclareLaunchArgument(
                "gstreamer_inspect_path", default_value="gst-inspect-1.0"
            ),
            DeclareLaunchArgument("gstreamer_jpeg_parser", default_value="jpegparse"),
            DeclareLaunchArgument(
                "gstreamer_jpeg_caps",
                default_value="image/jpeg,framerate=@fps_fraction",
            ),
            DeclareLaunchArgument("gstreamer_jpeg_decoder", default_value="jpegdec"),
            DeclareLaunchArgument(
                "gstreamer_video_converter", default_value="videoconvert"
            ),
            DeclareLaunchArgument("gstreamer_video_scaler", default_value="videoscale"),
            DeclareLaunchArgument(
                "gstreamer_raw_caps",
                default_value=(
                    "video/x-raw,format=I420,width=@width,height=@height,"
                    "framerate=@fps_fraction"
                ),
            ),
            DeclareLaunchArgument("gstreamer_h264_encoder", default_value="x264enc"),
            DeclareLaunchArgument(
                "gstreamer_decoder_properties_json", default_value="{}"
            ),
            DeclareLaunchArgument(
                "gstreamer_converter_properties_json", default_value="{}"
            ),
            DeclareLaunchArgument(
                "gstreamer_encoder_properties_json",
                default_value=(
                    '{"bitrate":"@bitrate_kbps","byte-stream":true,'
                    '"key-int-max":"@gop","speed-preset":"ultrafast",'
                    '"tune":"zerolatency"}'
                ),
            ),
            Node(
                package="ros_image_rtp_adapter",
                executable="image_rtp_adapter",
                name="image_rtp_adapter",
                output="screen",
                parameters=[
                    {
                        "image_topic": LaunchConfiguration("image_topic"),
                        "input_message_type": LaunchConfiguration(
                            "input_message_type"
                        ),
                        "raw_encoding": LaunchConfiguration("raw_encoding"),
                        "source_id": LaunchConfiguration("source_id"),
                        "frame_id": LaunchConfiguration("frame_id"),
                        "rtp_host": LaunchConfiguration("rtp_host"),
                        "rtp_port": ParameterValue(
                            LaunchConfiguration("rtp_port"),
                            value_type=int,
                        ),
                        "control_socket": LaunchConfiguration("control_socket"),
                        "width": ParameterValue(LaunchConfiguration("width"), value_type=int),
                        "height": ParameterValue(LaunchConfiguration("height"), value_type=int),
                        "fps": ParameterValue(LaunchConfiguration("fps"), value_type=float),
                        "bitrate": ParameterValue(LaunchConfiguration("bitrate"), value_type=int),
                        "encoder_backend": LaunchConfiguration("encoder_backend"),
                        "encoder": LaunchConfiguration("encoder"),
                        "ffmpeg_path": LaunchConfiguration("ffmpeg_path"),
                        "ffmpeg_encoder_args_json": ParameterValue(
                            LaunchConfiguration("ffmpeg_encoder_args_json"), value_type=str
                        ),
                        "ffmpeg_video_filter": ParameterValue(
                            LaunchConfiguration("ffmpeg_video_filter"), value_type=str
                        ),
                        "gstreamer_path": LaunchConfiguration("gstreamer_path"),
                        "gstreamer_inspect_path": LaunchConfiguration(
                            "gstreamer_inspect_path"
                        ),
                        "gstreamer_jpeg_parser": LaunchConfiguration(
                            "gstreamer_jpeg_parser"
                        ),
                        "gstreamer_jpeg_caps": ParameterValue(
                            LaunchConfiguration("gstreamer_jpeg_caps"), value_type=str
                        ),
                        "gstreamer_jpeg_decoder": LaunchConfiguration(
                            "gstreamer_jpeg_decoder"
                        ),
                        "gstreamer_video_converter": LaunchConfiguration(
                            "gstreamer_video_converter"
                        ),
                        "gstreamer_video_scaler": LaunchConfiguration(
                            "gstreamer_video_scaler"
                        ),
                        "gstreamer_raw_caps": ParameterValue(
                            LaunchConfiguration("gstreamer_raw_caps"), value_type=str
                        ),
                        "gstreamer_h264_encoder": LaunchConfiguration(
                            "gstreamer_h264_encoder"
                        ),
                        "gstreamer_decoder_properties_json": ParameterValue(
                            LaunchConfiguration("gstreamer_decoder_properties_json"),
                            value_type=str,
                        ),
                        "gstreamer_converter_properties_json": ParameterValue(
                            LaunchConfiguration("gstreamer_converter_properties_json"),
                            value_type=str,
                        ),
                        "gstreamer_encoder_properties_json": ParameterValue(
                            LaunchConfiguration("gstreamer_encoder_properties_json"),
                            value_type=str,
                        ),
                    }
                ],
            ),
        ]
    )
