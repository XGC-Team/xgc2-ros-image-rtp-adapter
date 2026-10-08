"""Real private XRPC endpoints: native postconditions, CAS and capture faults."""
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
import email.parser
import email.policy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from PIL import Image
from xgc2_xrpc import Client, Fault, Runtime, PolicyError

from ros_image_rtp_adapter.control_socket import SnapshotCapture, SourceControlServer, SourceDescription
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
    with adapter(tmp_path, source_clock_domain="simulation") as (source, client, _):
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
                                replace(current[0], frame_id=""), b"legacy-bytes-only"):
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


def test_frame_queue_has_both_count_and_byte_admission(tmp_path, monkeypatch):
    payload = jpeg()
    monkeypatch.setattr("ros_image_rtp_adapter.runtime.MAX_QUEUED_BYTES", 2 * len(payload))
    source = ImageRtpAdapterRuntime(AdapterSettings.from_mapping({"control_socket": str(tmp_path / "s.sock"),
                                    "drop_to_latest": False}), encoder_factory=NativeEncoder)
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
