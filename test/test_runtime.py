from ros_image_rtp_adapter.runtime import ImageRtpAdapterRuntime, shutdown_signal_owner
from ros_image_rtp_adapter.settings import AdapterSettings
import threading
import time
from io import BytesIO
from PIL import Image
import pytest
import os
import signal


def jpeg_frame(index=0, *, width=16, height=16):
    output = BytesIO()
    Image.new("RGB", (width, height), ((index * 19) % 256, (index * 31) % 256,
                              (index * 53) % 256)).save(output, format="JPEG")
    return output.getvalue()


class FakeEncoder:
    def __init__(self):
        self.frames = []
        self.running = False
        self.diagnostic = ""
        self.preflight_calls = 0

    def preflight(self):
        self.preflight_calls += 1

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def write_frame(self, frame, source_stamp_ns=None):
        self.frames.append(frame if source_stamp_ns is None else (frame, source_stamp_ns))

    def set_access_unit_callback(self, callback):
        self.callback = callback

    def request_keyframe(self):
        pass


def make_runtime(tmp_path, **overrides):
    tmp_path.chmod(0o700)
    values = {
        "source_id": "test",
        "control_socket": str(tmp_path / "source.sock"),
        "width": 16,
        "height": 16,
        "fps": 10.0,
    }
    values.update(overrides)
    settings = AdapterSettings.from_mapping(values)
    encoder = FakeEncoder()
    runtime = ImageRtpAdapterRuntime(
        settings, encoder_factory=lambda **_kwargs: encoder
    )
    return runtime, encoder


def test_runtime_encodes_each_fresh_compressed_frame_once(tmp_path):
    runtime, encoder = make_runtime(tmp_path)
    jpeg = jpeg_frame()

    runtime.set_active(True)
    assert runtime.submit_compressed(jpeg, "jpeg")
    assert runtime.pump()
    assert not runtime.pump()
    assert encoder.frames == [jpeg]
    assert runtime.snapshot_jpeg() == jpeg
    assert runtime.snapshot_parts(False) == (jpeg, b"")


def test_fresh_snapshot_waits_for_the_next_latest_frame(tmp_path):
    runtime, _encoder = make_runtime(tmp_path, fps=20.0)
    first = jpeg_frame(1)
    second = jpeg_frame(2)
    runtime.submit_compressed(first, "jpeg")

    result = []
    waiter = threading.Thread(
        target=lambda: result.append(runtime.snapshot_parts(False, True))
    )
    waiter.start()
    time.sleep(0.03)
    assert result == []
    runtime.submit_compressed(second, "jpeg")
    waiter.join(timeout=1.0)
    assert result == [(second, b"")]


def test_compressed_snapshot_is_exact_passthrough_without_raw_jpeg_encoding(
    tmp_path, monkeypatch
):
    runtime, _encoder = make_runtime(tmp_path)
    sentinel = jpeg_frame(3)

    def unexpected_raw_encode(*_args, **_kwargs):
        raise AssertionError("compressed snapshot must not invoke raw JPEG encoding")

    monkeypatch.setattr(
        "ros_image_rtp_adapter.frames.RawFrame.to_jpeg", unexpected_raw_encode
    )
    runtime.submit_compressed(sentinel, "jpeg")
    assert runtime.snapshot_parts(False, False) == (sentinel, b"")


def test_runtime_set_active_discards_pending_but_accepts_fresh_recovery(tmp_path):
    runtime, encoder = make_runtime(tmp_path)
    old = jpeg_frame(4)
    fresh = jpeg_frame(5)

    runtime.set_active(True)
    runtime.submit_compressed(old, "jpeg")
    runtime.set_active(False)
    assert not runtime.pump()
    runtime.set_active(True)
    assert not runtime.pump()
    runtime.submit_compressed(fresh, "jpeg")
    assert runtime.pump()
    assert encoder.frames == [fresh]


def test_runtime_accepts_explicit_packed_raw_input(tmp_path):
    runtime, encoder = make_runtime(
        tmp_path,
        image_topic="/camera/image_raw",
        input_message_type="raw",
        raw_encoding="mono8",
    )
    raw = bytes(range(16)) * 16

    runtime.set_active(True)
    assert runtime.submit_raw(
        raw, width=16, height=16, step=16, encoding="mono8"
    )
    assert runtime.pump()
    assert encoder.frames == [raw]
    assert runtime.snapshot_jpeg().startswith(b"\xff\xd8")


def test_runtime_default_inactive_releases_encoder_on_stop(tmp_path):
    runtime, encoder = make_runtime(tmp_path)
    jpeg = jpeg_frame()

    assert runtime.submit_compressed(jpeg, "jpeg")
    assert not runtime.pump()
    assert not encoder.running

    runtime.set_active(True)
    assert encoder.running
    assert runtime.submit_compressed(jpeg, "jpeg")
    assert runtime.pump()

    runtime.set_active(False)
    assert not encoder.running
    assert not runtime.pump()


def test_runtime_start_preflights_without_allocating_encoder(tmp_path):
    runtime, encoder = make_runtime(tmp_path)

    runtime.start()
    try:
        assert encoder.preflight_calls == 1
        assert not encoder.running
        assert runtime.status()["rtp"]["active"] is False
    finally:
        runtime.stop()


def test_started_runtime_pumps_each_arriving_frame_without_a_same_rate_poll_timer(tmp_path):
    runtime, encoder = make_runtime(tmp_path, fps=30.0)
    frames = [jpeg_frame(index) for index in range(20)]

    runtime.start()
    try:
        runtime.set_active(True)
        for frame in frames:
            assert runtime.submit_compressed(frame, "jpeg")
            deadline = time.monotonic() + 0.5
            while len(encoder.frames) < len(frames[: frames.index(frame) + 1]) and time.monotonic() < deadline:
                time.sleep(0.001)
        deadline = time.monotonic() + 1.0
        while len(encoder.frames) < len(frames) and time.monotonic() < deadline:
            time.sleep(0.001)
        assert encoder.frames == frames
        assert runtime.status()["rtp"]["frames_dropped"] == 0
    finally:
        runtime.stop()


def test_pump_cannot_release_its_own_runtime_owner(tmp_path):
    runtime, encoder = make_runtime(tmp_path)
    observed = threading.Event()
    errors = []
    def fail_write(_frame):
        raise RuntimeError("injected pump failure")
    def stop_from_pump(_message):
        try:
            runtime.stop()
        except RuntimeError as error:
            errors.append(error)
        observed.set()
    encoder.write_frame = fail_write
    runtime._log_error = stop_from_pump
    runtime.start()
    try:
        runtime.set_active(True)
        pump = runtime._rtp.thread
        assert runtime.submit_compressed(jpeg_frame(), "jpeg")
        assert observed.wait(1)
        assert errors and "cannot join itself" in str(errors[0])
        assert runtime._control is not None and runtime._rpc_runtime is not None
        assert runtime._rtp.thread is pump
        pump.join(1)
        assert not pump.is_alive()
        runtime.stop()
        assert runtime._control is None and runtime._rtp.thread is None
    finally:
        runtime.stop()


def test_unstarted_pump_is_cleaned_after_thread_start_failure(tmp_path, monkeypatch):
    runtime, _encoder = make_runtime(tmp_path)
    original_start = threading.Thread.start
    def fail_pump_start(thread):
        if thread.name == "image-rtp-encoder-pump":
            raise RuntimeError("injected thread start failure")
        original_start(thread)
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_pump_start)
        with pytest.raises(RuntimeError, match="thread start failure"):
            runtime.start()
    assert runtime._control is None and runtime._rpc_runtime is None
    assert runtime._rtp.thread is None and not runtime._started
    runtime.start()
    runtime.stop()


def test_graceful_signal_owner_requests_stop_and_restores_handlers():
    original = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    with shutdown_signal_owner() as requested:
        assert not requested.is_set()
        os.kill(os.getpid(), signal.SIGTERM)
        assert requested.wait(0.2)
        os.kill(os.getpid(), signal.SIGINT)  # A repeated graceful request does not detach owners.
    assert {signum: signal.getsignal(signum) for signum in original} == original


def make_preview_runtime(tmp_path, **overrides):
    tmp_path.chmod(0o700)
    values = {
        "source_id": "test",
        "control_socket": str(tmp_path / "video.sock"),
        "width": 64,
        "height": 36,
        "video_width": 32,
        "video_height": 18,
        "video_bitrate": 400_000,
        "video_topic": "/camera/video_h264",
        "fps": 10.0,
    }
    values.update(overrides)
    settings = AdapterSettings.from_mapping(values)
    rtp, preview = FakeEncoder(), FakeEncoder()
    created = {}

    def preview_factory(**kwargs):
        created.update(kwargs)
        return preview

    runtime = ImageRtpAdapterRuntime(
        settings,
        encoder_factory=lambda **_kwargs: rtp,
        preview_encoder_factory=preview_factory,
        on_access_unit=lambda data, stamp: None,
    )
    return runtime, rtp, preview, created


def test_preview_encoder_gets_its_own_geometry_and_budget(tmp_path):
    _runtime, _rtp, preview, created = make_preview_runtime(tmp_path)

    assert (created["source_width"], created["source_height"]) == (64, 36)
    assert (created["width"], created["height"], created["bitrate"]) == (32, 18, 400_000)
    assert preview.callback is not None


def test_ros_preview_and_edge_start_and_stop_independent_encoders(tmp_path):
    runtime, rtp, preview, _created = make_preview_runtime(tmp_path)

    runtime.set_video_active(True)
    assert preview.running and not rtp.running  # AR alone never encodes the 4K RTP output.
    runtime.set_active(True)
    assert preview.running and rtp.running
    runtime.set_active(False)
    assert preview.running and not rtp.running  # Closing WebRTC must not stop ROS AR.
    runtime.set_active(True)
    runtime.set_video_active(False)
    assert rtp.running and not preview.running  # Closing AR must not stop WebRTC.
    runtime.set_active(False)
    assert not rtp.running and not preview.running


def test_each_active_encoder_receives_every_kept_frame(tmp_path):
    runtime, rtp, preview, _created = make_preview_runtime(tmp_path)
    runtime.set_active(True)
    runtime.set_video_active(True)
    jpeg = jpeg_frame(width=64, height=36)

    assert runtime.submit_compressed(jpeg, "jpeg", source_stamp_ns=10)
    assert runtime.pump()
    assert not runtime.pump()
    assert rtp.frames == [jpeg]
    assert preview.frames == [(jpeg, 10)]


def test_video_only_demand_leaves_the_rtp_queue_empty(tmp_path):
    runtime, rtp, preview, _created = make_preview_runtime(tmp_path)
    runtime.set_video_active(True)

    assert runtime.submit_compressed(jpeg_frame(6, width=64, height=36), "jpeg", source_stamp_ns=10)
    assert runtime.submit_compressed(jpeg_frame(7, width=64, height=36), "jpeg", source_stamp_ns=20)
    status = runtime.status()
    assert status["rtp"]["pending"] == 0 and status["rtp"]["frames_dropped"] == 0
    assert status["ros-preview"]["frames_dropped"] == 1
    assert runtime.pump()
    assert rtp.frames == []
    assert preview.frames == [(jpeg_frame(7, width=64, height=36), 20)]


def test_h264_timestamp_follows_kept_source_after_input_drop(tmp_path):
    runtime, _rtp, preview, _created = make_preview_runtime(tmp_path)
    runtime.set_video_active(True)
    jpeg = jpeg_frame(width=64, height=36)
    assert not runtime.submit_compressed(jpeg, "jpeg", source_stamp_ns=0)
    runtime.submit_compressed(jpeg, "jpeg", source_stamp_ns=10)
    runtime.submit_compressed(jpeg, "jpeg", source_stamp_ns=20)
    assert runtime.pump()
    assert preview.frames == [(jpeg, 20)]


def test_status_report_names_each_encoder(tmp_path):
    runtime, _rtp, _preview, _created = make_preview_runtime(tmp_path)
    runtime.set_video_active(True)

    report = dict((message.split()[0], level) for level, message in runtime.status_report())
    assert report == {"rtp": "info", "ros-preview": "info"}


def test_preview_geometry_is_validated_only_when_the_preview_is_configured(tmp_path):
    base = {"control_socket": str(tmp_path / "s.sock"), "width": 64, "height": 36}
    AdapterSettings.from_mapping(base)  # generic 1280x720 preview defaults stay unused
    for overrides, message in (
        ({"video_width": 128, "video_height": 72}, "upscale"),
        ({"video_width": 32, "video_height": 24}, "aspect ratio"),
        ({"video_width": 32, "video_height": 18, "encoder_backend": "gstreamer"}, "FFmpeg"),
    ):
        try:
            AdapterSettings.from_mapping({**base, "video_topic": "/v", **overrides})
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(f"{overrides} must be rejected")
