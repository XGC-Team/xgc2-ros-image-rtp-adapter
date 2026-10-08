"""Real private XRPC endpoints: native postconditions, CAS and capture faults."""
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
import email.parser
import email.policy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import types

import pytest
from PIL import Image
from xgc2_xrpc import Client, Fault, Runtime, PolicyError

from ros_image_rtp_adapter.control_socket import SnapshotCapture, SourceControlServer, SourceDescription
from ros_image_rtp_adapter.encoder import SubprocessEncoder, FFmpegH264PreviewEncoder
from ros_image_rtp_adapter.runtime import ImageRtpAdapterRuntime
from ros_image_rtp_adapter.settings import AdapterSettings, MAX_FRAME_BYTES, prepare_default_control_directory


class NativeEncoder:
    def __init__(self, **config):
        self.config, self.running, self.diagnostic = config, False, ""
        self.frames = []

    def preflight(self):
        if self.config.get("bitrate") == 777:
            raise RuntimeError("native configuration rejected")

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def write_frame(self, frame):
        self.frames.append(frame)


def jpeg(width=16, height=16, color=(15, 30, 45)):
    output = BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="JPEG")
    return output.getvalue()


@contextmanager
def adapter(tmp_path, **values):
    settings = AdapterSettings.from_mapping({"control_socket": str(tmp_path / "source.sock"),
        "width": 16, "height": 16, "source_id": "camera", **values})
    source = ImageRtpAdapterRuntime(settings, encoder_factory=NativeEncoder)
    source.start()
    try:
        with Runtime() as runtime, Client(settings.control_socket, runtime=runtime) as client:
            discovery = client.json("/v1/describe", method="GET")
            client.instance_id = discovery["service_ref"]["instance_id"]
            yield source, client, discovery
    finally:
        source.stop()


def call(client, suffix, body=None, method="POST"):
    return client.json("/v1/media/sources/camera/" + suffix, body, method=method)


def test_native_transition_config_cas_and_failure_atomicity(tmp_path):
    with adapter(tmp_path) as (source, client, discovery):
        assert discovery["sources"] == ["camera"]
        assert "sourceId" not in discovery
        assert call(client, "status", method="GET")["state"] == "idle"
        first = call(client, "config", method="GET")
        assert first["desired_revision"] == first["applied_revision"] == 1
        assert first["persisted_revision"] is None
        applied = call(client, "config", {"expected_revision": 1, "persist": False,
                                          "config": {"rtp_port": 5501, "bitrate": 400000}}, "PATCH")
        assert applied["desired"] == applied["applied"] == source.configuration()
        assert source.encoder.config["rtp_port"] == 5501
        assert source.encoder.config["bitrate"] == 400000
        assert applied["applied_revision"] == 2
        descriptor = call(client, "describe", method="GET")
        assert descriptor["rtpPort"] == 5501
        for expected, config, code in ((1, {"bitrate": 500000}, "conflict"),
                                      (2, {"bitrate": 777}, "unavailable"),
                                      (2, {"width": 32}, "restart_required"),
                                      (2, {"bogus": 1}, "invalid_argument"),
                                      (2, {"rtp_port": True}, "invalid_argument")):
            with pytest.raises(Fault) as error:
                call(client, "config", {"expected_revision": expected, "persist": False,
                                        "config": config}, "PATCH")
            assert error.value.code == code
            assert call(client, "config", method="GET") == applied
        for active in (True, False):
            receipt = call(client, "start" if active else "stop", {})
            assert receipt["completion"] == "applied"
            assert source.encoder.running is active
            assert receipt["active"] is active
            assert call(client, "status", method="GET")["applied_active"] is active
            if active:
                with pytest.raises(Fault) as error:
                    call(client, "config", {"expected_revision": 2, "persist": False,
                                            "config": {"bitrate": 500000}}, "PATCH")
                assert error.value.code == "conflict"
        with pytest.raises(Fault):
            call(client, "config", {"expected_revision": 2, "persist": True,
                                    "config": {"bitrate": 500000}}, "PATCH")
        for path in ("/v1/set-active", "/v1/snapshot", "/v1/request-keyframe"):
            with pytest.raises(Fault) as error:
                client.json(path, {})
            assert error.value.code == "not_found"


def test_real_capture_same_frame_and_no_fabricated_calibration(tmp_path):
    with adapter(tmp_path, source_clock_domain="simulation", width=32, height=16,
                 frame_id="optical-real") as (source, client, _):
        payload = jpeg(width=32, height=16)
        assert source.submit_compressed(payload, "jpeg", source_stamp_ns=0, frame_id="optical-real")
        response = client.call("/v1/media/sources/camera/capture", {"snapshotId": "same-frame",
                               "includeRgb": True, "requireFresh": False, "requestKeyframe": False})
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            ("Content-Type: " + response.content_type + "\r\n\r\n").encode() + response.body)
        parts = list(message.iter_parts())
        metadata = json.loads(parts[0].get_payload(decode=True))
        assert (metadata["width"], metadata["height"]) == (32, 16)
        assert metadata["frameId"] == "optical-real" and metadata["frameSequence"] == 1
        descriptor = call(client, "describe", method="GET")
        assert (metadata["width"], metadata["height"], metadata["frameId"]) == (
            descriptor["width"], descriptor["height"], descriptor["frameId"])
        assert metadata["timestampNanoseconds"] == 0 and metadata["timestampClockDomain"] == "simulation"
        assert metadata["calibrationState"] == "unavailable"
        assert "cameraMatrix" not in metadata and "distortion" not in metadata
        assert parts[1].get_payload(decode=True) == payload
        assert len(parts[2].get_payload(decode=True)) == 32 * 16 * 3
        assert all(part["Content-Transfer-Encoding"] is None for part in parts)
        assert not call(client, "describe", method="GET")["keyframeRequestSupported"]
        with pytest.raises(Fault) as error:
            call(client, "request-keyframe", {})
        assert error.value.code == "unsupported"
        with pytest.raises(Fault) as error:
            call(client, "capture", {"requestKeyframe": True})
        assert error.value.code == "unsupported"


def test_wait_script_uses_service_discovery_and_fenced_source_descriptor(tmp_path):
    with adapter(tmp_path) as (source, _client, _):
        script = Path(__file__).parents[1] / "scripts/wait_describe.py"
        result = subprocess.run([sys.executable, str(script), "--socket", source.settings.control_socket,
                                 "--source-id", "camera", "--rtp-port", "5004", "--timeout", "2"],
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=4)
        descriptor = json.loads(result.stdout)
        assert descriptor["sourceId"] == "camera" and descriptor["codec"] == "H264"


def test_capture_provider_identity_and_geometry_are_checked(tmp_path):
    description = SourceDescription("camera", "127.0.0.1", 5004, 16, 16, 10, "optical")
    current = [SnapshotCapture(jpeg(), width=16, height=16, frame_id="optical", frame_sequence=1)]
    with Runtime() as runtime:
        source = SourceControlServer(str(tmp_path / "c.sock"), description, runtime=runtime,
            on_snapshot=lambda *_args: current[0])
        source.start()
        try:
            with Client(str(tmp_path / "c.sock"), runtime=runtime) as client:
                client.instance_id = client.json("/v1/describe", method="GET")["service_ref"]["instance_id"]
                for invalid in (replace(current[0], width=32), replace(current[0], rgb=b"wrong"),
                                replace(current[0], frame_sequence=0), replace(current[0], jpeg=b"bad"),
                                replace(current[0], frame_id=""), replace(current[0], frame_id="other-optical"),
                                replace(current[0], jpeg=jpeg(32, 16), width=32), b"legacy-bytes-only"):
                    current[0] = invalid
                    with pytest.raises(Fault):
                        call(client, "capture", {"includeRgb": True})
                    current[0] = SnapshotCapture(jpeg(), width=16, height=16,
                                                frame_id="optical", frame_sequence=1)
        finally:
            source.stop()


def test_stopped_runtime_can_restart_with_fresh_instance(tmp_path):
    source = ImageRtpAdapterRuntime(AdapterSettings.from_mapping({"control_socket": str(tmp_path / "c.sock")}),
                                   encoder_factory=NativeEncoder)
    identities = []
    for _ in range(2):
        source.start()
        try:
            with Runtime() as runtime, Client(source.settings.control_socket, runtime=runtime) as client:
                identities.append(client.json("/v1/describe", method="GET")["service_ref"]["instance_id"])
        finally:
            source.stop()
    assert identities[0] != identities[1]
    assert source._rpc_runtime is None and source._control is None


def test_default_endpoint_private_directory_and_symlink_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    settings = AdapterSettings.from_mapping({"source_id": "private"})
    assert settings.control_socket == str(tmp_path / "xgc2-camera/private.sock")
    prepare_default_control_directory(settings.control_socket, settings.source_id)
    assert os.stat(tmp_path / "xgc2-camera").st_mode & 0o777 == 0o700
    (tmp_path / "xgc2-camera").rmdir()
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "xgc2-camera").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        prepare_default_control_directory(settings.control_socket, settings.source_id)
    assert not list(real.iterdir())


def test_bad_jpeg_and_large_input_never_enter_native_queue(tmp_path):
    source = ImageRtpAdapterRuntime(AdapterSettings.from_mapping({"control_socket": str(tmp_path / "s.sock")}),
                                   encoder_factory=NativeEncoder)
    assert source._rpc_runtime is None
    assert not source.submit_compressed(b"\xff\xd8fake\xff\xd9", "jpeg")
    assert not source.submit_compressed(bytes(MAX_FRAME_BYTES + 1), "jpeg")
    assert source.status()["frames_in"] == 0


@pytest.mark.parametrize("input_type", ["compressed", "raw"])
def test_input_rejects_geometry_or_nonempty_frame_identity_mismatch(tmp_path, input_type):
    with adapter(tmp_path, input_message_type=input_type, raw_encoding="rgb8",
                 frame_id="optical-real") as (source, client, _):
        def submit(width=16, height=16, frame_id="optical-real", stamp=10):
            if input_type == "compressed":
                return source.submit_compressed(jpeg(width, height), "jpeg",
                                                frame_id=frame_id, source_stamp_ns=stamp)
            return source.submit_raw(bytes(width * height * 3), width=width, height=height,
                                     step=width * 3, encoding="rgb8", frame_id=frame_id,
                                     source_stamp_ns=stamp)
        assert submit()
        assert not submit(width=32, stamp=20)
        assert not submit(frame_id="other-optical", stamp=30)
        assert source.status()["frames_in"] == 1
        response = client.call("/v1/media/sources/camera/capture", {"includeRgb": True})
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            ("Content-Type: " + response.content_type + "\r\n\r\n").encode() + response.body)
        metadata = json.loads(next(message.iter_parts()).get_payload(decode=True))
        assert (metadata["width"], metadata["height"], metadata["frameId"]) == (16, 16, "optical-real")
        assert metadata["frameSequence"] == 1 and metadata["timestampNanoseconds"] == 10
        # An absent ROS header identity uses the fixed configured frame; a
        # conflicting nonempty header is never rewritten into that identity.
        assert submit(frame_id="", stamp=40)


def test_frame_queue_has_both_count_and_byte_admission(tmp_path, monkeypatch):
    payload = jpeg()
    monkeypatch.setattr("ros_image_rtp_adapter.runtime.MAX_QUEUED_BYTES", 2 * len(payload))
    source = ImageRtpAdapterRuntime(AdapterSettings.from_mapping({"control_socket": str(tmp_path / "s.sock"),
                                    "drop_to_latest": False, "width": 16, "height": 16}),
                                   encoder_factory=NativeEncoder)
    source.set_active(True)
    for _ in range(5):
        assert source.submit_compressed(payload, "jpeg")
    status = source.status()["rtp"]
    assert status["pending"] == 2 and status["pending_bytes"] == 2 * len(payload)
    assert status["frames_dropped"] == 3
    source.set_active(False)
    assert source.status()["rtp"]["pending_bytes"] == 0


def test_fixed_encoder_profile_does_not_advertise_mutable_bitrate(tmp_path):
    with adapter(tmp_path, ffmpeg_encoder_args_json='["-b:v","400000"]') as (_source, client, _):
        config = call(client, "config", method="GET")
        assert config["mutable_fields"] == ["rtp_host", "rtp_port"]
        assert "bitrate" not in config["applied"]
        with pytest.raises(Fault) as error:
            call(client, "config", {"expected_revision": 1, "persist": False,
                                    "config": {"bitrate": 800000}}, "PATCH")
        assert error.value.code == "restart_required"


def test_failed_native_stop_does_not_claim_applied_or_hide_live_encoder(tmp_path):
    with adapter(tmp_path) as (source, client, _):
        call(client, "start", {})
        real_stop = source.encoder.stop
        source.encoder.stop = lambda: None
        try:
            with pytest.raises(Fault) as error:
                call(client, "stop", {})
            assert error.value.code == "unavailable"
            status = call(client, "status", method="GET")
            assert status["state"] == "faulted"
            assert status["native"]["rtp"]["encoder_running"]
            assert status["applied_active"]
        finally:
            source.encoder.stop = real_stop
        assert call(client, "stop", {})["completion"] == "applied"
        assert not source.encoder.running


def test_capture_cancellation_retains_single_conversion_slot_until_real_completion(tmp_path):
    entered, release = threading.Event(), threading.Event()
    with adapter(tmp_path) as (source, client, _):
        source.submit_compressed(jpeg(), "jpeg", source_stamp_ns=10)
        original = source.snapshot_capture
        def blocked_capture(*args):
            entered.set()
            release.wait(2)
            return original(*args)
        source._control._on_snapshot = blocked_capture
        errors = []
        def waiting_call():
            try:
                client.call("/v1/media/sources/camera/capture", {"includeRgb": False}, timeout=0.15)
            except Exception as error:
                errors.append(error)
        pending = threading.Thread(target=waiting_call)
        pending.start()
        try:
            assert entered.wait(1)
            pending.join(1)
            assert errors and not pending.is_alive()
            with pytest.raises(Fault) as error:
                call(client, "capture", {"includeRgb": False})
            assert error.value.code == "resource_exhausted"
        finally:
            release.set()
            pending.join(1)


def test_sdk_policy_snapshot_is_shared_and_environment_is_not_reread(tmp_path, monkeypatch):
    monkeypatch.setenv("XGC2_XRPC_HOST_MAX_CONNECTIONS", "3")
    monkeypatch.setenv("XGC2_XRPC_MAX_RESPONSE_BYTES", "65536")
    source = ImageRtpAdapterRuntime(AdapterSettings.from_mapping({"control_socket": str(tmp_path / "p.sock")}),
                                   encoder_factory=NativeEncoder)
    monkeypatch.setenv("XGC2_XRPC_HOST_MAX_CONNECTIONS", "7")
    source.start()
    try:
        assert source._rpc_runtime.policy is source._xrpc_policy
        assert source._control._host.runtime is source._rpc_runtime
        assert source._rpc_runtime.max_connections == source._control._host.limits.connections == 3
        assert source._control._host.limits.response_bytes == 65536
        field = source.status()["xrpc"]["effective_policy"]["fields"]["HOST_MAX_CONNECTIONS"]
        assert field["source"] == "environment" and field["value"] == 3 and field["ceiling"] == 8
    finally:
        source.stop()


@pytest.mark.parametrize("name,value", [("GRPC_MAX_STREAMS_PER_CONNECTION", "1"),
                                      ("UNDECLARED", "1"), ("HOST_MAX_CONNECTIONS", "9"),
                                      ("HOST_MAX_CONNECTIONS", "01")])
def test_unsupported_or_invalid_prefix_fails_before_native_allocation(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv("XGC2_XRPC_" + name, value)
    created = []
    with pytest.raises(PolicyError):
        ImageRtpAdapterRuntime(AdapterSettings.from_mapping({"control_socket": str(tmp_path / "p.sock")}),
                               encoder_factory=lambda **_kwargs: created.append(True))
    assert not created


def test_lost_start_receipt_keeps_pending_native_work_and_applies_once(tmp_path):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    with adapter(tmp_path) as (source, client, _):
        original_start = source.encoder.start
        executions = []
        def blocked_start():
            executions.append(True)
            entered.set()
            release.wait(2)
            original_start()
            finished.set()
        source.encoder.start = blocked_start
        failures = []
        def call_start():
            try:
                client.call("/v1/media/sources/camera/start", {}, timeout=0.15)
            except Exception as error:
                failures.append(error)
        caller = threading.Thread(target=call_start)
        caller.start()
        try:
            assert entered.wait(1)
            caller.join(1)
            assert failures and not caller.is_alive()
            status = call(client, "status", method="GET")
            assert status["state"] == "starting" and status["desired_active"]
            assert not status["applied_active"]
            release.set()
            assert finished.wait(1)
            assert source._control._transition.acquire(timeout=1)
            source._control._transition.release()
            assert executions == [True]
            status = call(client, "status", method="GET")
            assert status["state"] == "active" and status["applied_active"]
            assert call(client, "stop", {})["completion"] == "applied"
        finally:
            release.set()
            caller.join(1)


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("native fixture did not reach its required state")
        time.sleep(0.005)


class SlowNativeChild(SubprocessEncoder):
    """A real child that acknowledges TERM and exits only on fixture release."""

    def __init__(self, directory):
        super().__init__()
        self.ready = directory / "child-ready"
        self.terminated = directory / "child-term"
        self.release = directory / "child-release"
        self.children = []
        self.write_entered = threading.Event()

    def validate_runtime(self):
        pass

    def _build_command(self):
        script = """
from pathlib import Path
import signal, sys, time
ready, terminated, release = map(Path, sys.argv[1:])
def acknowledge_term(_signal, _frame):
    terminated.write_text("TERM received")
signal.signal(signal.SIGTERM, acknowledge_term)
ready.write_text("native child ready")
while not release.exists():
    time.sleep(0.005)
"""
        return [sys.executable, "-u", "-c", script,
                str(self.ready), str(self.terminated), str(self.release)]

    def _on_launched(self, proc):
        self.children.append(proc)

    def _before_write(self, _stamp):
        self.write_entered.set()

    def start(self):
        super().start()
        wait_until(self.ready.exists)


@contextmanager
def native_child_adapter(tmp_path, **values):
    encoder = SlowNativeChild(tmp_path)
    settings = AdapterSettings.from_mapping({"control_socket": str(tmp_path / "n.sock"),
                                            "source_id": "camera", "width": 16, "height": 16,
                                            **values})
    source = ImageRtpAdapterRuntime(settings, encoder_factory=lambda **_kwargs: encoder)
    source.start()
    try:
        with Runtime() as runtime, Client(settings.control_socket, runtime=runtime) as client:
            client.instance_id = client.json("/v1/describe", method="GET")["service_ref"]["instance_id"]
            yield source, encoder, client
    finally:
        encoder.release.touch()
        source.stop()


def assert_endpoint_still_owned(source):
    with Runtime() as runtime:
        contender = SourceControlServer(source.settings.control_socket,
            SourceDescription("camera", "127.0.0.1", 5004, 16, 16, 10, "camera_optical"),
            runtime=runtime)
        try:
            with pytest.raises(FileExistsError, match="already has an owner"):
                contender.start()
        finally:
            contender.stop()


def assert_fresh_endpoint_can_start(source, old_instance):
    assert not Path(source.settings.control_socket).exists()
    with Runtime() as runtime:
        replacement = SourceControlServer(source.settings.control_socket,
            SourceDescription("camera", "127.0.0.1", 5004, 16, 16, 10, "camera_optical"),
            runtime=runtime)
        replacement.start()
        try:
            with Client(source.settings.control_socket, runtime=runtime) as client:
                fresh = client.json("/v1/describe", method="GET")["service_ref"]["instance_id"]
                assert fresh != old_instance
        finally:
            replacement.stop()


def test_native_stop_receipt_waits_for_real_child_reap(tmp_path):
    with native_child_adapter(tmp_path) as (source, encoder, client):
        call(client, "start", {})
        child = encoder.children[0]
        receipts, failures = [], []
        def stop_native():
            try:
                receipts.append(call(client, "stop", {}))
            except Exception as error:
                failures.append(error)
        stopper = threading.Thread(target=stop_native)
        stopper.start()
        try:
            wait_until(encoder.terminated.exists)
            status = call(client, "status", method="GET")
            assert status["state"] == "stopping" and status["applied_active"]
            assert status["native"]["rtp"]["encoder_running"]
            assert not status["native"]["rtp"]["encoder_stopped"]
            assert child.poll() is None and not receipts and stopper.is_alive()
        finally:
            encoder.release.touch()
            stopper.join(2)
        assert not stopper.is_alive() and not failures
        assert receipts[0]["completion"] == "applied" and not receipts[0]["active"]
        assert child.returncode == 0 and encoder.stopped
        assert not call(client, "status", method="GET")["applied_active"]


def test_runtime_shutdown_keeps_endpoint_until_native_quiescence(tmp_path):
    with native_child_adapter(tmp_path) as (source, encoder, client):
        call(client, "start", {})
        child = encoder.children[0]
        old_instance = client.instance_id
        old_runtime, old_control = source._rpc_runtime, source._control
        failures = []
        def stop_runtime():
            try:
                source.stop()
            except Exception as error:
                failures.append(error)
        stopper = threading.Thread(target=stop_runtime)
        stopper.start()
        try:
            wait_until(encoder.terminated.exists)
            assert child.poll() is None and not encoder.stopped and stopper.is_alive()
            assert source._rpc_runtime is old_runtime and source._control is old_control
            status = call(client, "status", method="GET")
            assert status["state"] == "stopping" and status["applied_active"]
            assert not status["desired_active"]
            for suffix, body, method in (
                ("start", {}, "POST"), ("stop", {}, "POST"),
                ("request-keyframe", {}, "POST"), ("capture", {}, "POST"),
                ("config", {"expected_revision": 1, "persist": False,
                            "config": {"bitrate": 300000}}, "PATCH"),
            ):
                with pytest.raises(Fault) as error:
                    call(client, suffix, body, method)
                assert error.value.code == "unavailable"
            assert not source.submit_compressed(jpeg(), "jpeg")
            assert_endpoint_still_owned(source)
        finally:
            encoder.release.touch()
            stopper.join(2)
        assert not stopper.is_alive() and not failures
        assert child.returncode == 0 and encoder.stopped
        assert source._rpc_runtime is None and source._control is None
        assert all(channel.thread is None for channel in source._channels)
        assert_fresh_endpoint_can_start(source, old_instance)


def test_runtime_shutdown_releases_real_blocked_pump_before_endpoint(tmp_path):
    import array
    import fcntl
    import termios
    with native_child_adapter(tmp_path, input_message_type="raw", raw_encoding="rgb8",
                              width=256, height=256) as (source, encoder, client):
        call(client, "start", {})
        child = encoder.children[0]
        pump = source._rtp.thread
        readers = list(encoder._readers)
        frame = bytes(256 * 256 * 3)
        capacity = fcntl.fcntl(child.stdin.fileno(), fcntl.F_GETPIPE_SZ)
        assert len(frame) > capacity
        assert source.submit_raw(frame, width=256, height=256, step=256 * 3, encoding="rgb8")
        assert encoder.write_entered.wait(1)
        def pipe_is_full():
            pending = array.array("i", [0])
            fcntl.ioctl(child.stdin.fileno(), termios.FIONREAD, pending)
            return pending[0] == capacity
        wait_until(pipe_is_full)
        assert pump.is_alive() and source._rtp.lock.locked()
        with pytest.raises(Fault) as error:
            client.json("/v1/media/sources/camera/config",
                        {"expected_revision": 1, "persist": False, "config": {"bitrate": 300000}},
                        method="PATCH", timeout=0.5)
        assert error.value.code == "conflict"  # Never wait behind the blocked pipe writer.
        old_instance = client.instance_id
        failures = []
        def stop_runtime():
            try:
                source.stop()
            except Exception as error:
                failures.append(error)
        stopper = threading.Thread(target=stop_runtime)
        stopper.start()
        try:
            wait_until(encoder.terminated.exists)
            assert child.poll() is None and stopper.is_alive()
            assert source._control._domain_jobs == 0
            assert call(client, "status", method="GET")["applied_active"]
            assert_endpoint_still_owned(source)
        finally:
            encoder.release.touch()
            stopper.join(2)
        assert not stopper.is_alive() and not failures
        assert child.returncode == 0 and encoder.stopped
        assert not pump.is_alive() and all(not reader.is_alive() for reader in readers)
        assert len(encoder.children) == 1  # Stop-interrupted write did not replace its child.
        assert source._rtp.thread is None and source._control is None
        assert_fresh_endpoint_can_start(source, old_instance)


def test_failed_reap_retains_native_runtime_and_endpoint_for_stop_retry(tmp_path, monkeypatch):
    with native_child_adapter(tmp_path) as (source, encoder, client):
        call(client, "start", {})
        child = encoder.children[0]
        old_runtime, old_control = source._rpc_runtime, source._control
        old_instance = client.instance_id
        original_wait, original_kill = child.wait, child.kill
        waits, kills = [], []
        def fail_wait(timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired(child.args, timeout)
        monkeypatch.setattr(child, "wait", fail_wait)
        monkeypatch.setattr(child, "kill", lambda: kills.append(True))
        try:
            with pytest.raises(RuntimeError, match="not reaped"):
                source.stop()
            assert len(waits) == 2 and kills == [True]
            assert child.poll() is None and encoder._proc is child and not encoder.stopped
            assert source._rpc_runtime is old_runtime and source._control is old_control
            status = call(client, "status", method="GET")
            assert status["state"] == "faulted" and status["applied_active"]
            assert status["native"]["rtp"]["encoder_running"]
            with pytest.raises(RuntimeError, match="shutdown is incomplete"):
                source.start()
            with pytest.raises(Fault) as error:
                call(client, "start", {})
            assert error.value.code == "unavailable"
            assert_endpoint_still_owned(source)
        finally:
            monkeypatch.setattr(child, "wait", original_wait)
            monkeypatch.setattr(child, "kill", original_kill)
            encoder.release.touch()
        source.stop()
        assert child.returncode == 0 and encoder.stopped
        assert source._rpc_runtime is None and source._control is None
        assert_fresh_endpoint_can_start(source, old_instance)


def test_cancelled_accepted_start_finishes_before_shutdown_releases_endpoint(tmp_path):
    with native_child_adapter(tmp_path) as (source, encoder, client):
        entered, release_start, closing = threading.Event(), threading.Event(), threading.Event()
        original_start, original_begin_close = encoder.start, source._control.begin_close
        def gated_start():
            entered.set()
            if not release_start.wait(2):
                raise RuntimeError("fixture start was not released")
            original_start()
        def begin_close():
            original_begin_close()
            closing.set()
        encoder.start = gated_start
        source._control.begin_close = begin_close
        caller_failures, stop_failures = [], []
        def start_source():
            try:
                client.call("/v1/media/sources/camera/start", {}, timeout=0.15)
            except Exception as error:
                caller_failures.append(error)
        def stop_runtime():
            try:
                source.stop()
            except Exception as error:
                stop_failures.append(error)
        caller = threading.Thread(target=start_source)
        stopper = threading.Thread(target=stop_runtime)
        old_instance = client.instance_id
        caller.start()
        try:
            assert entered.wait(1)
            caller.join(1)
            assert caller_failures and not caller.is_alive()
            stopper.start()
            assert closing.wait(1)
            assert source._control._domain_jobs == 1 and stopper.is_alive()
            with pytest.raises(Fault) as error:
                call(client, "start", {})
            assert error.value.code == "unavailable"
            assert_endpoint_still_owned(source)
            release_start.set()
            wait_until(encoder.terminated.exists)
            assert len(encoder.children) == 1 and encoder.children[0].poll() is None
            assert source._control is not None and stopper.is_alive()
            assert_endpoint_still_owned(source)
        finally:
            release_start.set()
            encoder.release.touch()
            caller.join(2)
            if stopper.ident is not None:
                stopper.join(2)
        assert not caller.is_alive() and not stopper.is_alive() and not stop_failures
        assert len(encoder.children) == 1 and encoder.children[0].returncode == 0
        assert encoder.stopped and source._control is None and source._rpc_runtime is None
        assert_fresh_endpoint_can_start(source, old_instance)


def load_ros_wrapper(monkeypatch, filename, modules):
    """Mock ROS API ownership only; native child/readers remain real."""
    sensor = types.ModuleType("sensor_msgs")
    sensor.msg = types.ModuleType("sensor_msgs.msg")
    sensor.msg.CompressedImage = type("CompressedImage", (), {})
    sensor.msg.Image = type("Image", (), {})
    modules = {"sensor_msgs": sensor, "sensor_msgs.msg": sensor.msg, **modules}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).parents[1] / "ros_image_rtp_adapter" / filename
    spec = importlib.util.spec_from_file_location("_native_owner_" + path.stem, path)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    return wrapper


@contextmanager
def requested_shutdown():
    requested = threading.Event()
    requested.set()
    yield requested


def test_ros2_main_keeps_node_and_context_on_failed_stop_until_real_retry(tmp_path, monkeypatch):
    with native_child_adapter(tmp_path) as (source, encoder, client):
        call(client, "start", {})
        child = encoder.children[0]
        failed, destroyed, shutdown = threading.Event(), threading.Event(), threading.Event()
        messages, initialization = [], []
        def log_error(message):
            messages.append(message)
            failed.set()
        class RosNodeOwner:
            def get_logger(self):
                return types.SimpleNamespace(error=log_error)
            def destroy_node(self):
                assert encoder.stopped and child.returncode is not None
                destroyed.set()
                return True
        rclpy = types.ModuleType("rclpy")
        rclpy.node = types.ModuleType("rclpy.node")
        rclpy.node.Node = RosNodeOwner
        rclpy.signals = types.ModuleType("rclpy.signals")
        rclpy.signals.SignalHandlerOptions = types.SimpleNamespace(NO=object())
        rclpy.init = lambda **values: initialization.append(values)
        rclpy.ok = lambda: True
        def shutdown_context():
            assert destroyed.is_set() and encoder.stopped
            shutdown.set()
        rclpy.shutdown = shutdown_context
        wrapper = load_ros_wrapper(monkeypatch, "node.py", {"rclpy": rclpy,
            "rclpy.node": rclpy.node, "rclpy.signals": rclpy.signals})
        node = wrapper.ImageRtpAdapterNode.__new__(wrapper.ImageRtpAdapterNode)
        node._runtime, node._destroyed = source, False
        wrapper.ImageRtpAdapterNode = lambda: node
        wrapper.shutdown_signal_owner = requested_shutdown
        original_wait, original_kill = child.wait, child.kill
        monkeypatch.setattr(child, "wait", lambda timeout=None: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(child.args, timeout)))
        monkeypatch.setattr(child, "kill", lambda: None)
        failures = []
        def run_main():
            try:
                wrapper.main()
            except BaseException as error:
                failures.append(error)
        main_owner = threading.Thread(target=run_main)
        main_owner.start()
        try:
            assert failed.wait(1)
            assert main_owner.is_alive() and not destroyed.is_set() and not shutdown.is_set()
            assert node._runtime is source and encoder._proc is child and not encoder.stopped
            assert source._control is not None and source._rpc_runtime is not None
            assert not node._destroyed
            assert initialization == [{"args": None,
                "signal_handler_options": rclpy.signals.SignalHandlerOptions.NO}]
            assert_endpoint_still_owned(source)
        finally:
            monkeypatch.setattr(child, "wait", original_wait)
            monkeypatch.setattr(child, "kill", original_kill)
            encoder.release.touch()
            main_owner.join(2)
        assert not main_owner.is_alive() and not failures
        assert destroyed.is_set() and shutdown.is_set() and node._destroyed
        assert encoder.stopped and child.returncode == 0
        assert any("process owner must terminate" in message for message in messages)


class NativePreviewCallbackEncoder(FFmpegH264PreviewEncoder):
    """Real stdout native work; this fixture tests ownership, not DDS/codecs."""
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


@pytest.mark.parametrize("shutdown_path", ["client-hook", "main-owner"])
def test_ros1_shutdown_retains_real_publish_callback_before_ros_teardown(tmp_path, monkeypatch, shutdown_path):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    failed, teardown = threading.Event(), threading.Event()
    messages, hooks, initialization = [], [], []
    class PublisherOwner:
        def __init__(self):
            self.destroyed = False
            self.publications = []
        def get_num_connections(self):
            return 1
        def publish(self, message):
            assert not self.destroyed
            self.publications.append(message)
            entered.set()
            assert release.wait(3)
            assert not self.destroyed
            completed.set()
    publisher = PublisherOwner()
    rospy = types.ModuleType("rospy")
    rospy.ROSInitException = RuntimeError
    rospy.is_shutdown = teardown.is_set
    def log_error(message, *args):
        messages.append(message % args if args else message)
        failed.set()
    rospy.logerr = log_error
    rospy.loginfo = rospy.logwarn = lambda *_args: None
    rospy.Publisher = lambda *_args, **_values: publisher
    rospy.Subscriber = rospy.Timer = lambda *_args, **_values: object()
    class Duration:
        def __init__(self, value):
            self.value = value
        @classmethod
        def from_sec(cls, value):
            return cls(value)
    rospy.Duration = Duration
    rospy.Time = lambda sec, nsec: (sec, nsec)
    rospy.on_shutdown = hooks.append
    rospy.init_node = lambda *args, **values: initialization.append((args, values))
    def shutdown_ros(_reason):
        # Noetic's client hooks precede its shutdown flag/internal teardown.
        for hook in hooks:
            hook()
        assert completed.is_set() and preview.stopped
        publisher.destroyed = True
        teardown.set()
    rospy.signal_shutdown = shutdown_ros
    foxglove = types.ModuleType("foxglove_msgs")
    foxglove.msg = types.ModuleType("foxglove_msgs.msg")
    foxglove.msg.CompressedVideo = type("CompressedVideo", (), {})
    wrapper = load_ros_wrapper(monkeypatch, "ros1_node.py", {"rospy": rospy,
        "foxglove_msgs": foxglove, "foxglove_msgs.msg": foxglove.msg})
    values = {"control_socket": str(tmp_path / "p.sock"), "source_id": "camera",
              "width": 16, "height": 16, "video_width": 16, "video_height": 16,
              "video_topic": "/video-preview"}
    settings = AdapterSettings.from_mapping(values)
    preview = NativePreviewCallbackEncoder(ffmpeg_path="unused", source_width=16,
        source_height=16, width=16, height=16, fps=settings.fps, bitrate=400000)
    source = ImageRtpAdapterRuntime(settings, encoder_factory=NativeEncoder,
        preview_encoder_factory=lambda **_values: preview, on_access_unit=lambda *_args: None)
    rospy.get_param = lambda name, default: values.get(name[1:], default)
    def existing_runtime(_settings, **options):
        preview.set_access_unit_callback(options["on_access_unit"])
        return source
    wrapper.ImageRtpAdapterRuntime = existing_runtime
    node = wrapper.ImageRtpAdapterROS1Node()
    node._update_video_consumer(None)
    child = preview._proc
    readers = list(preview._readers)
    image = types.SimpleNamespace(data=jpeg(), format="jpeg", header=types.SimpleNamespace(
        stamp=types.SimpleNamespace(to_nsec=lambda: 123), frame_id=settings.frame_id))
    node._on_compressed_image(image)
    assert entered.wait(1)
    original_remaining = preview._remaining
    monkeypatch.setattr(preview, "_remaining", lambda deadline: min(original_remaining(deadline), 0.05))
    if shutdown_path == "main-owner":
        wrapper.ImageRtpAdapterROS1Node = lambda: node
        wrapper.shutdown_signal_owner = requested_shutdown
        close = wrapper.main
    else:
        close = lambda: rospy.signal_shutdown("fixture shutdown request")
    failures = []
    def shutdown_owner():
        try:
            close()
        except BaseException as error:
            failures.append(error)
    owner = threading.Thread(target=shutdown_owner)
    owner.start()
    try:
        assert failed.wait(1)
        assert child.returncode is not None and not preview.running and not preview.stopped
        assert preview._proc is child and any(reader.is_alive() for reader in readers)
        assert owner.is_alive() and not teardown.is_set() and not publisher.destroyed
        assert node._runtime is source and node._video_publisher is publisher
        assert node._shutdown_started and source._control is not None
        assert_endpoint_still_owned(source)
        node._update_video_consumer(None)  # Shutdown never allocates a replacement child.
        assert preview._proc is child and len(publisher.publications) == 1
    finally:
        release.set()
        owner.join(2)
        source.stop()
    assert not owner.is_alive() and not failures
    assert completed.is_set() and teardown.is_set() and publisher.destroyed
    assert preview.stopped and all(not reader.is_alive() for reader in readers)
    assert source._control is None and source._rpc_runtime is None
    assert publisher.publications[0].timestamp == (0, 123)
    assert any("process owner must terminate" in message for message in messages)
    if shutdown_path == "main-owner":
        assert initialization == [(("image_rtp_adapter",), {"anonymous": False, "disable_signals": True})]


@pytest.mark.parametrize("failure_stage", ["runtime-start", "subscription", "timer"])
def test_ros2_failed_constructor_retains_actual_child_until_cleanup_retry(tmp_path, monkeypatch, failure_stage):
    """Constructor ownership with a real native child and mocked ROS context."""
    encoder = SlowNativeChild(tmp_path)
    values = {"control_socket": str(tmp_path / "constructor.sock"), "source_id": "camera",
              "width": 16, "height": 16}
    source = ImageRtpAdapterRuntime(AdapterSettings.from_mapping(values),
                                   encoder_factory=lambda **_values: encoder)
    source.set_active(True)
    child = encoder.children[0]
    failed, destroyed, shutdown = threading.Event(), threading.Event(), threading.Event()
    nodes, errors = [], []
    initial_error = RuntimeError("constructor %s failed" % failure_stage)
    def log_error(_message):
        failed.set()
    class RosNodeOwner:
        def __init__(self, _name):
            self.parameters = {}
            nodes.append(self)
        def declare_parameter(self, name, default):
            self.parameters[name] = values.get(name, default)
        def get_parameter(self, name):
            return types.SimpleNamespace(value=self.parameters[name])
        def get_logger(self):
            return types.SimpleNamespace(error=log_error, info=lambda *_args: None,
                                         warning=lambda *_args: None)
        def create_subscription(self, *_args):
            if failure_stage == "subscription":
                raise initial_error
            return object()
        def create_timer(self, *_args):
            if failure_stage == "timer":
                raise initial_error
            return object()
        def destroy_node(self):
            assert encoder.stopped and child.returncode is not None
            destroyed.set()
            return True
    rclpy = types.ModuleType("rclpy")
    rclpy.node = types.ModuleType("rclpy.node")
    rclpy.node.Node = RosNodeOwner
    rclpy.signals = types.ModuleType("rclpy.signals")
    rclpy.signals.SignalHandlerOptions = types.SimpleNamespace(NO=object())
    rclpy.init = lambda **_values: None
    def shutdown_context():
        assert destroyed.is_set() and encoder.stopped
        shutdown.set()
    rclpy.shutdown = shutdown_context
    wrapper = load_ros_wrapper(monkeypatch, "node.py", {"rclpy": rclpy,
        "rclpy.node": rclpy.node, "rclpy.signals": rclpy.signals})
    wrapper.ImageRtpAdapterRuntime = lambda *_args, **_values: source
    wrapper.shutdown_signal_owner = requested_shutdown
    if failure_stage == "runtime-start":
        original_create = source._create_control
        def create_failing_control():
            original_create()
            original_start = source._control.start
            def bind_then_fail():
                original_start()
                raise initial_error
            source._control.start = bind_then_fail
        source._create_control = create_failing_control
    original_wait, original_kill = child.wait, child.kill
    def failed_wait(timeout=None):
        raise subprocess.TimeoutExpired(child.args, timeout)
    monkeypatch.setattr(child, "wait", failed_wait)
    monkeypatch.setattr(child, "kill", lambda: None)
    def construct_through_main():
        try:
            wrapper.main()
        except BaseException as error:
            errors.append(error)
    owner = threading.Thread(target=construct_through_main)
    owner.start()
    try:
        assert failed.wait(1)
        assert owner.is_alive() and not destroyed.is_set() and not shutdown.is_set()
        assert len(nodes) == 1 and nodes[0]._runtime is source and not nodes[0]._destroyed
        assert encoder._proc is child and child.poll() is None and not encoder.stopped
        assert source._control is not None and source._rpc_runtime is not None
        assert_endpoint_still_owned(source)
        assert not errors  # __init__ cannot raise back to main before real cleanup.
    finally:
        monkeypatch.setattr(child, "wait", original_wait)
        monkeypatch.setattr(child, "kill", original_kill)
        encoder.release.touch()
        owner.join(2)
        source.stop()
    assert not owner.is_alive() and destroyed.is_set() and shutdown.is_set()
    assert encoder.stopped and child.returncode == 0 and nodes[0]._destroyed
    assert source._control is None and source._rpc_runtime is None
    assert len(errors) == 1
    if failure_stage == "runtime-start":
        assert "not reaped" in str(errors[0])
        assert exception_context_contains(errors[0], initial_error)
    else:
        assert errors[0] is initial_error


@pytest.mark.parametrize("failure_stage", ["runtime-start", "subscription", "timer"])
def test_ros1_failed_constructor_retains_actual_publish_callback_until_cleanup(tmp_path, monkeypatch, failure_stage):
    """Real stdout/publish callback; mocked ROS APIs are not a DDS acceptance."""
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    failed, teardown = threading.Event(), threading.Event()
    nodes, hooks, errors = [], [], []
    initial_error = RuntimeError("constructor %s failed" % failure_stage)
    class PublisherOwner:
        destroyed = False
        def publish(self, _message):
            assert not self.destroyed
            entered.set()
            assert release.wait(3)
            assert not self.destroyed
            completed.set()
    publisher = PublisherOwner()
    rospy = types.ModuleType("rospy")
    rospy.ROSInitException = RuntimeError
    rospy.is_shutdown = teardown.is_set
    rospy.init_node = lambda *_args, **_values: None
    rospy.loginfo = rospy.logwarn = lambda *_args: None
    rospy.logerr = lambda *_args: failed.set()
    rospy.Publisher = lambda *_args, **_values: publisher
    rospy.Time = lambda sec, nsec: (sec, nsec)
    rospy.on_shutdown = hooks.append
    def subscribe(*_args, **_values):
        if failure_stage == "subscription":
            raise initial_error
        return object()
    def timer(*_args):
        if failure_stage == "timer":
            raise initial_error
        return object()
    rospy.Subscriber, rospy.Timer = subscribe, timer
    class Duration:
        def __init__(self, value):
            self.value = value
        @classmethod
        def from_sec(cls, value):
            return cls(value)
    rospy.Duration = Duration
    def shutdown_ros(_reason):
        for hook in hooks:
            hook()
        assert completed.is_set() and preview.stopped
        publisher.destroyed = True
        teardown.set()
    rospy.signal_shutdown = shutdown_ros
    foxglove = types.ModuleType("foxglove_msgs")
    foxglove.msg = types.ModuleType("foxglove_msgs.msg")
    foxglove.msg.CompressedVideo = type("CompressedVideo", (), {})
    wrapper = load_ros_wrapper(monkeypatch, "ros1_node.py", {"rospy": rospy,
        "foxglove_msgs": foxglove, "foxglove_msgs.msg": foxglove.msg})
    values = {"control_socket": str(tmp_path / "constructor.sock"), "source_id": "camera",
              "width": 16, "height": 16, "video_width": 16, "video_height": 16,
              "video_topic": "/video-preview"}
    settings = AdapterSettings.from_mapping(values)
    preview = NativePreviewCallbackEncoder(ffmpeg_path="unused", source_width=16,
        source_height=16, width=16, height=16, fps=settings.fps, bitrate=400000)
    source = ImageRtpAdapterRuntime(settings, encoder_factory=NativeEncoder,
        preview_encoder_factory=lambda **_values: preview, on_access_unit=lambda *_args: None)
    rospy.get_param = lambda name, default: values.get(name[1:], default)
    def existing_runtime(_settings, **options):
        nodes.append(options["on_access_unit"].__self__)
        preview.set_access_unit_callback(options["on_access_unit"])
        return source
    wrapper.ImageRtpAdapterRuntime = existing_runtime
    wrapper.shutdown_signal_owner = requested_shutdown
    original_remaining = preview._remaining
    monkeypatch.setattr(preview, "_remaining", lambda deadline: min(original_remaining(deadline), 0.05))
    original_create = source._create_control
    def create_control_with_live_callback():
        original_create()
        original_start = source._control.start
        def bind_and_publish():
            original_start()
            source.set_video_active(True)
            assert source.submit_compressed(jpeg(), "jpeg", source_stamp_ns=123,
                                            frame_id=settings.frame_id)
            assert entered.wait(1)
            if failure_stage == "runtime-start":
                raise initial_error
        source._control.start = bind_and_publish
    source._create_control = create_control_with_live_callback
    def construct_through_main():
        try:
            wrapper.main()
        except BaseException as error:
            errors.append(error)
    owner = threading.Thread(target=construct_through_main)
    owner.start()
    try:
        assert failed.wait(2)
        child = preview._proc
        assert child is not None and child.returncode is not None
        assert not preview.running and not preview.stopped
        assert owner.is_alive() and not teardown.is_set() and not publisher.destroyed
        assert len(nodes) == 1 and nodes[0]._runtime is source
        assert nodes[0]._video_publisher is publisher and nodes[0]._shutdown_started
        assert source._control is not None and source._rpc_runtime is not None
        assert_endpoint_still_owned(source)
        assert not errors  # Constructor/context ownership has not been abandoned.
    finally:
        release.set()
        owner.join(2)
        source.stop()
    assert not owner.is_alive() and completed.is_set() and teardown.is_set()
    assert publisher.destroyed and preview.stopped
    assert source._control is None and source._rpc_runtime is None
    assert len(errors) == 1
    if failure_stage == "runtime-start":
        assert "reader did not quiesce" in str(errors[0])
        assert exception_context_contains(errors[0], initial_error)
    else:
        assert errors[0] is initial_error


def exception_context_contains(error, expected):
    seen = set()
    while error is not None and id(error) not in seen:
        if error is expected:
            return True
        seen.add(id(error))
        error = error.__context__
    return False
