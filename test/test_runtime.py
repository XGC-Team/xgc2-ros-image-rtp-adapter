from ros_image_rtp_adapter.runtime import ImageRtpAdapterRuntime
from ros_image_rtp_adapter.settings import AdapterSettings


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

    def write_frame(self, frame):
        self.frames.append(frame)

    def request_keyframe(self):
        pass


def make_runtime(tmp_path, **overrides):
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
    jpeg = b"\xff\xd8frame\xff\xd9"

    runtime.set_active(True)
    assert runtime.submit_compressed(jpeg, "jpeg")
    assert runtime.pump()
    assert not runtime.pump()
    assert encoder.frames == [jpeg]
    assert runtime.snapshot_jpeg() == jpeg


def test_runtime_set_active_discards_pending_but_accepts_fresh_recovery(tmp_path):
    runtime, encoder = make_runtime(tmp_path)
    old = b"\xff\xd8old\xff\xd9"
    fresh = b"\xff\xd8fresh\xff\xd9"

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
    jpeg = b"\xff\xd8frame\xff\xd9"

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
        assert runtime.status()["active"] is False
    finally:
        runtime.stop()
