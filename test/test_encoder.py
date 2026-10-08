from unittest.mock import Mock, patch
import subprocess
import signal
import sys
import threading
import time

import pytest

from ros_image_rtp_adapter.encoder import (
    FFmpegH264PreviewEncoder,
    FFmpegRtpEncoder,
    GStreamerRtpEncoder,
    SubprocessEncoder,
    create_h264_preview_encoder,
    create_rtp_encoder,
)


def make_encoder(*, encoder="libx264"):
    return FFmpegRtpEncoder(
        ffmpeg_path="ffmpeg",
        rtp_host="127.0.0.1",
        rtp_port=5004,
        width=1280,
        height=720,
        fps=15.0,
        bitrate=2_500_000,
        encoder=encoder,
    )


def make_gstreamer_encoder(**overrides):
    values = {
        "gstreamer_path": "gst-launch-1.0",
        "gstreamer_inspect_path": "gst-inspect-1.0",
        "rtp_host": "127.0.0.1",
        "rtp_port": 5004,
        "width": 1280,
        "height": 720,
        "fps": 15.0,
        "bitrate": 2_500_000,
    }
    values.update(overrides)
    return GStreamerRtpEncoder(**values)


def test_soft_encoder_uses_one_second_repeated_header_gop():
    command = make_encoder()._build_command()

    assert command[command.index("-g") + 1] == "15"
    assert command[command.index("-keyint_min") + 1] == "15"
    assert command[command.index("-x264-params") + 1] == "repeat-headers=1:scenecut=0"


def test_keyframe_request_does_not_restart_live_ffmpeg_process():
    encoder = make_encoder()
    process = Mock()
    process.stdin = Mock()
    process.stdin.write.return_value = 4
    encoder._proc = process

    with patch("ros_image_rtp_adapter.encoder.subprocess.Popen") as popen:
        encoder.request_keyframe()
        encoder.write_frame(b"jpeg")

    popen.assert_not_called()
    process.stdin.write.assert_called_once_with(b"jpeg")
    process.stdin.flush.assert_called_once_with()


def test_non_x264_encoder_does_not_receive_x264_only_options():
    command = make_encoder(encoder="h264_nvenc")._build_command()

    assert "-x264-params" not in command


def test_nvenc_uses_a_bounded_low_latency_burst_contract():
    command = make_encoder(encoder="h264_nvenc")._build_command()

    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-preset") + 1] == "p4"
    assert command[command.index("-tune") + 1] == "ll"
    assert command[command.index("-rc") + 1] == "cbr"
    assert command[command.index("-multipass") + 1] == "qres"
    assert command[command.index("-maxrate") + 1] == "2500000"
    assert command[command.index("-bufsize") + 1] == "2500000"
    assert command[command.index("-bf") + 1] == "0"
    assert command[command.index("-delay") + 1] == "2"
    assert command[command.index("-strict_gop") + 1] == "1"
    assert command[command.index("-no-scenecut") + 1] == "1"


def test_ffmpeg_raw_input_is_explicit_fixed_size_and_never_roundtrips_through_jpeg():
    command = FFmpegRtpEncoder(
        ffmpeg_path="ffmpeg",
        rtp_host="127.0.0.1",
        rtp_port=5004,
        width=640,
        height=360,
        fps=10.0,
        bitrate=1_000_000,
        input_format="bgr8",
    )._build_command()

    assert command[command.index("-f") + 1] == "rawvideo"
    assert command[command.index("-pixel_format") + 1] == "bgr24"
    assert command[command.index("-video_size") + 1] == "640x360"
    assert "mjpeg" not in command


def test_ffmpeg_custom_arguments_expand_backend_neutral_runtime_markers():
    encoder = FFmpegRtpEncoder(
        ffmpeg_path="ffmpeg",
        rtp_host="127.0.0.1",
        rtp_port=5004,
        width=640,
        height=360,
        fps=12.5,
        bitrate=3_000_000,
        encoder="some_h264_encoder",
        encoder_args='["-b:v","@bitrate","-g","@gop","-profile:v","main"]',
    )

    command = encoder._build_command()

    assert command[command.index("-b:v") + 1] == "3000000"
    assert command[command.index("-g") + 1] == "12"
    assert command[command.index("-profile:v") + 1] == "main"
    assert "-preset" not in command


def test_gstreamer_default_is_a_software_pipeline_with_expanded_caps():
    command = make_gstreamer_encoder()._build_command()

    assert command[0] == "gst-launch-1.0"
    assert "jpegdec" in command
    assert "videoconvert" in command
    assert "videoscale" in command
    assert "x264enc" in command
    assert "image/jpeg,framerate=15/1" in command
    assert (
        "video/x-raw,format=I420,width=1280,height=720,framerate=15/1" in command
    )
    assert "bitrate=2500" in command
    assert "key-int-max=15" in command
    assert "host=127.0.0.1" in command
    assert "port=5004" in command


def test_gstreamer_vendor_elements_are_profile_data_not_code_branches():
    command = make_gstreamer_encoder(
        jpeg_decoder="nvjpegdec",
        video_converter="nvvidconv",
        video_scaler="identity",
        raw_caps=(
            "video/x-raw(memory:NVMM),format=NV12,width=@width,"
            "height=@height,framerate=@fps_fraction"
        ),
        h264_encoder="nvv4l2h264enc",
        encoder_properties=(
            '{"control-rate":1,"bitrate":"@bitrate",'
            '"iframeinterval":"@gop","idrinterval":"@gop",'
            '"insert-sps-pps":true}'
        ),
    )._build_command()

    assert "nvjpegdec" in command
    assert "nvvidconv" in command
    assert "identity" in command
    assert "nvv4l2h264enc" in command
    assert "bitrate=2500000" in command
    assert "iframeinterval=15" in command
    assert "idrinterval=15" in command
    assert "insert-sps-pps=true" in command


def test_gstreamer_raw_input_uses_rawvideoparse_before_the_configured_converter():
    command = make_gstreamer_encoder(input_format="rgba8")._build_command()

    assert "rawvideoparse" in command
    assert "format=rgba" in command
    assert "width=1280" in command
    assert "height=720" in command
    assert command.index("rawvideoparse") < command.index("videoconvert")
    assert "jpegparse" not in command
    assert "jpegdec" not in command


def test_gstreamer_rejects_pipeline_injection_in_element_or_caps():
    with pytest.raises(ValueError, match="element factory"):
        make_gstreamer_encoder(h264_encoder="x264enc ! fakesink")
    with pytest.raises(ValueError, match="pipeline separator"):
        make_gstreamer_encoder(raw_caps="video/x-raw ! fakesink")


def test_json_configuration_rejects_non_scalar_gstreamer_properties():
    with pytest.raises(ValueError, match="string, number, or boolean"):
        make_gstreamer_encoder(encoder_properties='{"options":["unsafe"]}')


def test_backend_factory_never_auto_detects_hardware():
    encoder = create_rtp_encoder(
        backend="ffmpeg",
        ffmpeg_path="ffmpeg",
        rtp_host="127.0.0.1",
        rtp_port=5004,
        width=640,
        height=360,
        fps=10.0,
        bitrate=1_000_000,
    )
    assert isinstance(encoder, FFmpegRtpEncoder)

    with pytest.raises(ValueError, match="ffmpeg, gstreamer"):
        create_rtp_encoder(backend="auto")


def test_gstreamer_preflight_reports_the_missing_element():
    encoder = make_gstreamer_encoder()
    launcher = Mock(returncode=0, stderr=b"")
    unavailable = Mock(returncode=1, stderr=b"No such element")

    with patch(
        "ros_image_rtp_adapter.encoder.subprocess.run",
        side_effect=[launcher, unavailable],
    ):
        with pytest.raises(RuntimeError, match="fdsrc"):
            encoder.validate_runtime()


def test_gstreamer_preflight_rejects_unknown_configured_properties():
    encoder = make_gstreamer_encoder(encoder_properties='{"not-a-property":1}')
    launcher = Mock(returncode=0, stderr=b"")
    inspection = Mock(returncode=0, stdout=b"Element Properties:\n  name : name\n")

    with patch(
        "ros_image_rtp_adapter.encoder.subprocess.run",
        side_effect=[launcher] + [inspection] * 16,
    ):
        with pytest.raises(RuntimeError, match="not-a-property"):
            encoder.validate_runtime()


def test_encoder_finishes_short_pipe_writes_before_accepting_next_frame():
    encoder = make_encoder()
    process = Mock()
    process.stdin.write.side_effect = [2, 2]
    encoder._proc = process
    encoder.write_frame(b"jpeg")
    assert [bytes(call.args[0]) for call in process.stdin.write.call_args_list] == [b"jpeg", b"eg"]
    process.stdin.flush.assert_called_once_with()


def make_preview(**overrides):
    values = {
        "ffmpeg_path": "ffmpeg",
        "source_width": 3840,
        "source_height": 2160,
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "bitrate": 8_000_000,
        "encoder": "h264_nvenc",
    }
    values.update(overrides)
    return FFmpegH264PreviewEncoder(**values)


def test_rtp_output_never_carries_the_ros_preview_pipe():
    command = make_encoder(encoder="h264_nvenc")._build_command()

    assert command[-3:] == ["-payload_type", "96", "rtp://127.0.0.1:5004?pkt_size=1200"]
    assert "pipe:1" not in command and "tee" not in command
    assert "-lowres" not in command
    # Starts on the first frame, as the former shared tee command did.
    assert command[command.index("-probesize") + 1] == "32"
    assert command[command.index("-analyzeduration") + 1] == "0"
    assert command.index("-probesize") < command.index("-i")


def test_preview_decodes_an_exact_half_size_jpeg_at_preview_resolution():
    command = make_preview()._build_command()

    lowres = command.index("-lowres")
    assert command[lowres + 1] == "1"
    assert lowres < command.index("-i")  # decoder option, not an output option
    assert command[command.index("-vf") + 1] == "scale=1920:1080:flags=area"
    assert "pad" not in command[command.index("-vf") + 1]
    assert command[command.index("-b:v") + 1] == "8000000"
    assert command[command.index("-maxrate") + 1] == "8000000"
    assert command[command.index("-bufsize") + 1] == "8000000"
    assert command[command.index("-delay") + 1] == "2"
    assert command[command.index("-rc") + 1] == "cbr"
    assert command[command.index("-bsf:v") + 1] == "dump_extra=freq=keyframe,h264_metadata=aud=insert"
    assert command[-3:] == ["-f", "h264", "pipe:1"]
    assert not any(argument.startswith("rtp://") for argument in command)


def test_preview_area_averages_other_ratios_and_raw_input():
    other_ratio = make_preview(source_width=2560, source_height=1440)._build_command()
    raw = make_preview(input_format="bgr8")._build_command()

    assert "-lowres" not in other_ratio and "-lowres" not in raw
    assert other_ratio[other_ratio.index("-vf") + 1] == "scale=1920:1080:flags=area"
    # Raw frames are read at the configured source size, then downscaled.
    assert raw[raw.index("-video_size") + 1] == "3840x2160"
    assert raw[raw.index("-vf") + 1] == "scale=1920:1080:flags=area"
    assert raw[raw.index("-delay") + 1] == "0"


def test_preview_requires_source_timestamps_and_the_ffmpeg_backend():
    with pytest.raises(ValueError, match="timestamp"):
        make_preview().write_frame(b"jpeg")
    with pytest.raises(ValueError, match="FFmpeg"):
        create_h264_preview_encoder(backend="gstreamer")


def test_stop_releases_a_writer_blocked_on_a_stalled_encoder():
    import threading

    encoder = make_encoder()
    released = threading.Event()
    blocked = threading.Event()

    class StalledPipe:
        def write(self, _data):
            blocked.set()
            released.wait(5)
            raise ValueError("write to closed file")

        def flush(self):
            pass

        def close(self):
            released.set()

    process = Mock()
    process.stdin = StalledPipe()
    process.poll.return_value = 0
    encoder._proc = process
    writer = threading.Thread(target=encoder.write_frame, args=(b"frame",))
    writer.start()
    assert blocked.wait(1)
    with patch("ros_image_rtp_adapter.encoder.subprocess.Popen") as popen:
        encoder.stop()  # must not wait for the blocked writer's lock
        writer.join(timeout=2)
    assert not writer.is_alive()
    popen.assert_not_called()  # a stopped encoder is not restarted by the failed write
    assert encoder._proc is None


class LocalProcessEncoder(SubprocessEncoder):
    """A real pipe-fed child requiring no media backend or device."""

    def __init__(self, code="import time; time.sleep(60)"):
        super().__init__()
        self.code = code
        self.launches = 0
        self.write_entered = threading.Event()

    def validate_runtime(self):
        pass

    def _build_command(self):
        return [sys.executable, "-u", "-c", self.code]

    def _before_launch(self):
        self.launches += 1

    def _before_write(self, _stamp):
        self.write_entered.set()


def test_real_stalled_child_is_reaped_without_waiting_behind_its_writer():
    encoder = LocalProcessEncoder()
    encoder.start()
    proc = encoder._proc
    readers = list(encoder._readers)
    writer = threading.Thread(target=encoder.write_frame, args=(b"x" * (8 * 1024 * 1024),))
    try:
        writer.start()
        assert encoder.write_entered.wait(1)
        assert writer.is_alive()  # The child never consumes its bounded pipe.
        started = time.monotonic()
        encoder.stop()
        assert time.monotonic() - started < 2
        writer.join(1)
        assert not writer.is_alive()
        assert proc.returncode is not None
        assert encoder.stopped and not encoder.running
        assert all(not reader.is_alive() for reader in readers)
        assert encoder.launches == 1  # Interrupted write did not restart it.
    finally:
        encoder.stop()
        writer.join(1)


def test_real_child_ignoring_term_is_killed_and_reaped_before_stop_returns():
    encoder = LocalProcessEncoder(
        "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stderr.write('ready\\n'); sys.stderr.flush(); time.sleep(60)"
    )
    encoder.start()
    proc = encoder._proc
    try:
        deadline = time.monotonic() + 2
        while "ready" not in encoder.diagnostic and time.monotonic() < deadline:
            time.sleep(0.005)
        assert "ready" in encoder.diagnostic
        encoder.stop()
        assert proc.returncode == -signal.SIGKILL
        assert encoder.stopped and encoder._proc is None
    finally:
        encoder.stop()


def test_final_reap_failure_retains_owner_and_blocks_start_until_retry():
    encoder = LocalProcessEncoder()
    encoder.start()
    proc = encoder._proc
    try:
        with patch.object(proc, "wait", side_effect=subprocess.TimeoutExpired(proc.args, 0)):
            with pytest.raises(RuntimeError, match="not reaped"):
                encoder.stop()
            assert encoder._proc is proc
            assert not encoder.stopped
            with pytest.raises(RuntimeError, match="retry stop"):
                encoder.start()
            encoder.write_frame(b"ignored while stopping")
            assert encoder.launches == 1
        encoder.stop()
        assert proc.returncode is not None
        assert encoder._proc is None and encoder.stopped
    finally:
        encoder.stop()


def test_restart_reaps_old_native_owner_and_readers_before_replacement():
    encoder = LocalProcessEncoder()
    encoder.start()
    proc = encoder._proc
    readers = list(encoder._readers)
    try:
        proc.stdin.close()  # The still-live child makes the next write fail.
        encoder.write_frame(b"frame")
        assert proc.returncode is not None
        assert all(not reader.is_alive() for reader in readers)
        assert encoder._proc is not proc and encoder.running
        assert encoder.launches == 2
    finally:
        encoder.stop()


def test_restart_reap_failure_never_allocates_a_second_child():
    encoder = LocalProcessEncoder()
    encoder.start()
    proc = encoder._proc
    try:
        proc.stdin.close()
        with patch.object(proc, "wait", side_effect=OSError("injected wait failure")):
            with pytest.raises(RuntimeError, match="could not be reaped"):
                encoder.write_frame(b"frame")
            assert encoder._proc is proc and not encoder.stopped
            assert encoder.launches == 1
        encoder.stop()
        assert encoder.stopped
    finally:
        encoder.stop()


def test_start_during_real_child_stop_is_rejected_even_after_child_exit():
    encoder = LocalProcessEncoder()
    encoder.start()
    proc = encoder._proc
    wait_entered, wait_release = threading.Event(), threading.Event()
    wait = proc.wait
    errors = []

    def held_wait(*args, **kwargs):
        wait_entered.set()
        assert wait_release.wait(2)
        return wait(*args, **kwargs)

    def stop():
        try:
            encoder.stop()
        except BaseException as exc:
            errors.append(exc)

    stopper = threading.Thread(target=stop)
    try:
        with patch.object(proc, "wait", side_effect=held_wait):
            stopper.start()
            assert wait_entered.wait(1)
            # Reaping the child does not release encoder ownership while stop
            # is still working on readers and writer completion.
            assert wait(timeout=1) is not None
            assert not encoder.running and not encoder.stopped
            with pytest.raises(RuntimeError, match="transition"):
                encoder.start()
            assert encoder.launches == 1
            wait_release.set()
            stopper.join(2)
        assert not stopper.is_alive() and not errors
        assert encoder.stopped
    finally:
        wait_release.set()
        stopper.join(2)
        encoder.stop()


def test_preview_callback_completion_is_required_even_after_child_reap():
    class LocalPreview(FFmpegH264PreviewEncoder):
        def validate_runtime(self):
            pass

        def _build_command(self):
            return [sys.executable, "-u", "-c", (
                "import sys,time; sys.stdin.buffer.read(1); "
                "sys.stdout.buffer.write(b'\\x00\\x00\\x00\\x01\\x09\\xf0'"
                "b'\\x00\\x00\\x00\\x01\\x65payload'"
                "b'\\x00\\x00\\x00\\x01\\x09\\xf0'); "
                "sys.stdout.buffer.flush(); time.sleep(60)"
            )]

    encoder = LocalPreview(
        ffmpeg_path="unused", source_width=2, source_height=2,
        width=2, height=2, fps=1, bitrate=1000,
    )
    entered, release = threading.Event(), threading.Event()
    emitted = []

    def callback(unit, stamp):
        emitted.append((unit, stamp))
        entered.set()
        assert release.wait(3)

    encoder.set_access_unit_callback(callback)
    encoder.start()
    proc = encoder._proc
    readers = list(encoder._readers)
    remaining = encoder._remaining
    try:
        encoder.write_frame(b"x", 123)
        assert entered.wait(1)
        with patch.object(encoder, "_remaining", side_effect=lambda end: min(remaining(end), 0.05)):
            with pytest.raises(RuntimeError, match="reader did not quiesce"):
                encoder.stop()
        assert proc.returncode is not None and not encoder.running
        assert encoder._proc is proc and not encoder.stopped
        assert any(reader.is_alive() for reader in readers)
        with pytest.raises(RuntimeError, match="retry stop"):
            encoder.start()
        release.set()
        encoder.stop()
        assert encoder.stopped and all(not reader.is_alive() for reader in readers)
        assert len(emitted) == 1 and emitted[0][1] == 123
    finally:
        release.set()
        encoder.stop()


def test_reader_launch_failure_keeps_native_owner_for_stop():
    class FailingReaderEncoder(LocalProcessEncoder):
        def _on_launched(self, _proc):
            raise RuntimeError("injected reader launch failure")

    encoder = FailingReaderEncoder()
    try:
        with pytest.raises(RuntimeError, match="reader launch failure"):
            encoder.start()
        proc = encoder._proc
        assert proc is not None and not encoder.stopped
        with pytest.raises(RuntimeError, match="retry stop"):
            encoder.start()
        encoder.stop()
        assert proc.returncode is not None and encoder.stopped
    finally:
        encoder.stop()
