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
                description="sensor_msgs/CompressedImage topic (JPEG). Fully parameterized.",
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
            DeclareLaunchArgument("encoder", default_value="libx264"),
            DeclareLaunchArgument("ffmpeg_path", default_value="ffmpeg"),
            Node(
                package="ros_image_rtp_adapter",
                executable="image_rtp_adapter",
                name="image_rtp_adapter",
                output="screen",
                parameters=[
                    {
                        "image_topic": LaunchConfiguration("image_topic"),
                        "source_id": LaunchConfiguration("source_id"),
                        "frame_id": LaunchConfiguration("frame_id"),
                        "rtp_host": LaunchConfiguration("rtp_host"),
                        "rtp_port": ParameterValue(LaunchConfiguration("rtp_port"), value_type=int),
                        "control_socket": LaunchConfiguration("control_socket"),
                        "width": ParameterValue(LaunchConfiguration("width"), value_type=int),
                        "height": ParameterValue(LaunchConfiguration("height"), value_type=int),
                        "fps": ParameterValue(LaunchConfiguration("fps"), value_type=float),
                        "bitrate": ParameterValue(LaunchConfiguration("bitrate"), value_type=int),
                        "encoder": LaunchConfiguration("encoder"),
                        "ffmpeg_path": LaunchConfiguration("ffmpeg_path"),
                    }
                ],
            ),
        ]
    )
