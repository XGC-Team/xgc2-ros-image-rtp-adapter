from unittest.mock import Mock, patch

from ros_image_rtp_adapter.encoder import FFmpegJpegRtpEncoder


def make_encoder(*, encoder="libx264"):
    return FFmpegJpegRtpEncoder(
        ffmpeg_path="ffmpeg",
        rtp_host="127.0.0.1",
        rtp_port=5004,
        width=1280,
        height=720,
        fps=15.0,
        bitrate=2_500_000,
        encoder=encoder,
    )


def test_soft_encoder_uses_one_second_repeated_header_gop():
    command = make_encoder()._build_command()

    assert command[command.index("-g") + 1] == "15"
    assert command[command.index("-keyint_min") + 1] == "15"
    assert command[command.index("-x264-params") + 1] == "repeat-headers=1:scenecut=0"


def test_keyframe_request_does_not_restart_live_ffmpeg_process():
    encoder = make_encoder()
    process = Mock()
    process.stdin = Mock()
    encoder._proc = process

    with patch("ros_image_rtp_adapter.encoder.subprocess.Popen") as popen:
        encoder.request_keyframe()
        encoder.write_jpeg(b"jpeg")

    popen.assert_not_called()
    process.stdin.write.assert_called_once_with(b"jpeg")
    process.stdin.flush.assert_called_once_with()


def test_non_x264_encoder_does_not_receive_x264_only_options():
    command = make_encoder(encoder="h264_nvenc")._build_command()

    assert "-x264-params" not in command
