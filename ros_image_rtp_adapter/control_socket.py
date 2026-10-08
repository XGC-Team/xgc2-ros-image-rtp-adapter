"""Camera-source domain control hosted by the shared XRPC Runtime/Host."""
from __future__ import annotations

import secrets
import socket
import threading
from functools import wraps
from dataclasses import dataclass
from typing import Dict

from xgc2_xrpc import Fault, Host, multipart
from ros_image_rtp_adapter.settings import MAX_FRAME_BYTES, MAX_FRAME_PIXELS
from ros_image_rtp_adapter.frames import FrameValidationError, jpeg_geometry

PROTOCOL_VERSION = 1


def _domain_operation(method):
    """Keep accepted native domain work owned after transport cancellation."""
    @wraps(method)
    def admitted(self, *args, **kwargs):
        with self._admission:
            if self._closing:
                raise Fault("unavailable", "camera source is stopping")
            self._domain_jobs += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            with self._admission:
                self._domain_jobs -= 1
                self._admission.notify_all()
    return admitted


@dataclass(frozen=True)
class SnapshotCapture:
    """Immutable bytes and identity from one retained ROS image."""
    jpeg: bytes
    rgb: bytes = b""
    timestamp_nanoseconds: int = 0
    timestamp_clock_domain: str = "unknown"
    width: int = 0
    height: int = 0
    frame_id: str = ""
    frame_sequence: int = 0


@dataclass
class SourceDescription:
    source_id: str
    rtp_host: str
    rtp_port: int
    width: int
    height: int
    fps: float
    frame_id: str
    snapshot_jpeg_policy: str = "source"
    snapshot_jpeg_backend: str = "source-jpeg-passthrough"
    snapshot_jpeg_hardware_state: str = "source-owned"
    capabilities: tuple = ("start", "stop", "capture", "fresh-snapshot")
    keyframe_request_supported: bool = False
    timestamp_clock_domain: str = "unknown"

    def as_dict(self) -> Dict:
        return {
            "ok": True, "protocolVersion": PROTOCOL_VERSION,
            "sourceId": self.source_id, "codec": "H264", "rtpPayloadType": 96,
            "rtpClockRate": 90000, "rtpHost": self.rtp_host, "rtpPort": self.rtp_port,
            "width": int(self.width), "height": int(self.height), "fps": float(self.fps),
            "frameId": self.frame_id, "snapshotJpegPolicy": self.snapshot_jpeg_policy,
            "snapshotJpegBackend": self.snapshot_jpeg_backend,
            "snapshotJpegHardwareState": self.snapshot_jpeg_hardware_state,
            "capabilities": list(self.capabilities), "timestampClockDomain": self.timestamp_clock_domain,
            "calibrationState": "unavailable",
            "keyframeRequestSupported": self.keyframe_request_supported,
            "keyframePolicy": "force-idr" if self.keyframe_request_supported else "bounded-gop",
        }


class SourceControlServer:
    """Own source transitions and captures; the SDK owns transport and leases."""
    def __init__(self, path, description, *, runtime, on_set_active=None,
                 on_request_keyframe=None, on_snapshot=None, on_status=None,
                 on_configuration=None, on_configure=None, snapshot_backend=None,
                 on_mutable_fields=None, snapshot_readback="latest-source-frame", instance_id=""):
        self._description = description
        self._instance_id = instance_id or secrets.token_hex(16)
        self.service_ref = {
            "target_id": socket.gethostname(), "service": "camera-source",
            "api_version": "v1", "instance_id": self._instance_id,
            "profile": "http.v1", "endpoint": {"kind": "unix", "address": path},
        }
        self._on_set_active, self._on_request_keyframe = on_set_active, on_request_keyframe
        self._on_snapshot, self._on_status = on_snapshot, on_status
        self._on_configuration, self._on_configure = on_configuration, on_configure
        self._on_mutable_fields = on_mutable_fields
        self._snapshot_backend = snapshot_backend or description.snapshot_jpeg_backend
        self._snapshot_readback = snapshot_readback
        self._active, self._desired_active, self._last_error = False, False, ""
        self._phase = ""
        self._revision = 1
        self._transition = threading.Lock()
        self._capture_slots = threading.BoundedSemaphore(1)
        self._admission = threading.Condition()
        self._closing = False
        self._domain_jobs = 0
        prefix = "/v1/media/sources/" + description.source_id
        self._host = Host(path, {
            ("GET", "/v1/describe"): self._discover,
            ("GET", "/v1/media/sources"): self._sources,
            ("GET", prefix + "/describe"): self._describe,
            ("GET", prefix + "/status"): self._status_route,
            ("GET", prefix + "/config"): self._configuration,
            ("PATCH", prefix + "/config"): self._configure,
            ("POST", prefix + "/start"): self._start_source,
            ("POST", prefix + "/stop"): self._stop_source,
            ("POST", prefix + "/request-keyframe"): self._keyframe,
            ("POST", prefix + "/capture"): self._capture,
        }, runtime=runtime, reclaim_unreachable=True, instance_id=self._instance_id,
            discovery_routes=("/v1/describe",))

    @property
    def active(self):
        return self._active

    def start(self):
        self._instance_id = secrets.token_hex(16)
        self.service_ref["instance_id"] = self._instance_id
        self._host.instance_id = self._instance_id
        self._host.start()

    def stop(self):
        self.begin_close()
        self.wait_quiescent()
        self._host.close()

    def begin_close(self):
        """Reject new mutations while retaining the listener and its lease."""
        with self._admission:
            self._closing = True
            self._desired_active = False

    def wait_quiescent(self, timeout=5.0):
        with self._admission:
            if not self._admission.wait_for(lambda: self._domain_jobs == 0, timeout):
                raise RuntimeError("source domain work did not quiesce; endpoint ownership retained")

    def close_failed(self, error):
        self._last_error = str(error)[:1024]

    @staticmethod
    def _request(request, allowed):
        if not isinstance(request, dict) or set(request) - set(allowed):
            raise Fault("invalid_argument", "unknown camera source request fields")
        return request

    def _discover(self, context, request):
        self._request(request, ())
        return {"ok": True, "service_ref": self.service_ref,
                "sources": [self._description.source_id], "management_api": "media-source.v1"}

    def _sources(self, context, request):
        self._request(request, ())
        return {"ok": True, "sources": [self._description.as_dict()]}

    def _status(self):
        native = self._on_status() if self._on_status else {}
        rtp = native.get("rtp", {})
        active = bool(rtp.get("active", self._active))
        healthy = active == bool(rtp.get("encoder_running", active))
        return {
            "ok": True, "source_id": self._description.source_id,
            "state": (self._phase or ("faulted" if (
                self._last_error and (self._desired_active != active or self._closing))
                else "stopping" if self._closing else "faulted" if not healthy
                else "active" if active else "idle")),
            "desired_active": self._desired_active, "applied_active": active,
            "configuration_revision": self._revision,
            "desired_revision": self._revision, "applied_revision": self._revision,
            "persisted_revision": None, "last_error": self._last_error, "native": native,
        }

    def _describe(self, context, request):
        self._request(request, ())
        return self._description.as_dict()

    def _status_route(self, context, request):
        self._request(request, ())
        return self._status()

    def _configuration_value(self):
        config = self._on_configuration() if self._on_configuration else {
            "rtp_host": self._description.rtp_host, "rtp_port": self._description.rtp_port,
        }
        return {
            "ok": True, "source_id": self._description.source_id,
            "desired": dict(config), "applied": dict(config),
            "desired_revision": self._revision, "applied_revision": self._revision,
            "persisted_revision": None, "persistence": "ephemeral",
            "mutable_fields": (self._on_mutable_fields() if self._on_mutable_fields
                               else ["rtp_host", "rtp_port", "bitrate"] if self._on_configure else []),
        }

    def _configuration(self, context, request):
        self._request(request, ())
        if not self._transition.acquire(timeout=context.remaining()):
            raise Fault("deadline_exceeded", "source transition deadline exceeded")
        try:
            context.check_cancelled()
            return self._configuration_value()
        finally:
            self._transition.release()

    @_domain_operation
    def _configure(self, context, request):
        self._request(request, ("expected_revision", "persist", "config"))
        expected = request.get("expected_revision")
        if type(expected) is not int or expected < 1:
            raise Fault("invalid_argument", "expected_revision must be a positive integer")
        if request.get("persist") is not False:
            raise Fault("invalid_argument", "source configuration supports only persist:false")
        config = request.get("config")
        if not isinstance(config, dict) or not config:
            raise Fault("invalid_argument", "config must be a non-empty object")
        if not self._transition.acquire(timeout=context.remaining()):
            raise Fault("deadline_exceeded", "source transition deadline exceeded")
        try:
            context.check_cancelled()
            if expected != self._revision:
                raise Fault("conflict", "configuration revision changed")
            if self._on_configure is None:
                raise Fault("unavailable", "source does not support online configuration")
            self._on_configure(config)
            self._revision += 1
            applied = self._on_configuration()
            self._description.rtp_host = applied["rtp_host"]
            self._description.rtp_port = applied["rtp_port"]
            return self._configuration_value()
        finally:
            self._transition.release()

    def _start_source(self, context, request):
        return self._set_active(context, request, True)

    def _stop_source(self, context, request):
        return self._set_active(context, request, False)

    @_domain_operation
    def _set_active(self, context, request, active):
        self._request(request, ())
        if not self._transition.acquire(timeout=context.remaining()):
            raise Fault("deadline_exceeded", "source transition deadline exceeded")
        try:
            context.check_cancelled()
            if not self._closing:
                self._desired_active = active
            if self._on_set_active is None:
                raise Fault("unavailable", "source has no native encoder control")
            try:
                self._phase = "starting" if active else "stopping"
                self._on_set_active(active)
                self._phase = ""
                self._active = active
                status = self._status()
                self._active = status["applied_active"]
                if status["applied_active"] != active or status["state"] == "faulted":
                    raise RuntimeError("native encoder postcondition was not applied")
            except Exception as error:
                self._last_error = str(error)[:1024]
                raise Fault("unavailable", "source transition failed: %s" % self._last_error) from error
            self._last_error = ""
            return {"ok": True, "source_id": self._description.source_id,
                    "state": status["state"], "active": active, "completion": "applied",
                    "configuration_revision": self._revision}
        finally:
            self._phase = ""
            self._transition.release()

    @_domain_operation
    def _keyframe(self, context, request):
        self._request(request, ())
        if not self._transition.acquire(timeout=context.remaining()):
            raise Fault("deadline_exceeded", "source transition deadline exceeded")
        try:
            context.check_cancelled()
            if not self._description.keyframe_request_supported or self._on_request_keyframe is None:
                raise Fault("unsupported", "source does not support force-IDR requests", status=501)
            self._on_request_keyframe()
            return {"ok": True, "source_id": self._description.source_id, "completion": "requested"}
        finally:
            self._transition.release()

    @_domain_operation
    def _capture(self, context, request):
        self._request(request, ("snapshotId", "includeRgb", "requireFresh", "requestKeyframe"))
        include_rgb = request.get("includeRgb", True)
        require_fresh = request.get("requireFresh", False)
        request_keyframe = request.get("requestKeyframe", False)
        for name, value in (("includeRgb", include_rgb), ("requireFresh", require_fresh),
                            ("requestKeyframe", request_keyframe)):
            if not isinstance(value, bool):
                raise Fault("invalid_argument", "%s must be a boolean" % name)
        snapshot_id = request.get("snapshotId", "")
        if not isinstance(snapshot_id, str) or len(snapshot_id.encode("utf-8")) > 128:
            raise Fault("invalid_argument", "snapshotId must be a bounded string")
        if request_keyframe and not self._description.keyframe_request_supported:
            raise Fault("unsupported", "capture source does not support force-IDR requests", status=501)
        if not self._capture_slots.acquire(blocking=False):
            raise Fault("resource_exhausted", "snapshot capture is busy")
        try:
            context.check_cancelled()
            if request_keyframe and self._on_request_keyframe:
                self._on_request_keyframe()
            capture = self._on_snapshot(include_rgb, require_fresh) if self._on_snapshot else None
            if capture is None:
                raise Fault("unavailable", "capture source has no requested frame")
            if not isinstance(capture, SnapshotCapture):
                raise Fault("internal", "capture provider did not return frame identity")
            jpeg, rgb = capture.jpeg, capture.rgb if include_rgb else b""
            if not isinstance(jpeg, bytes) or not isinstance(rgb, bytes):
                raise Fault("internal", "capture provider must retain immutable bytes")
            if not jpeg:
                raise Fault("unavailable", "capture source has no requested frame")
            width, height = capture.width, capture.height
            if (type(width) is not int or type(height) is not int or width < 1 or height < 1
                    or width * height > MAX_FRAME_PIXELS or len(jpeg) > MAX_FRAME_BYTES):
                raise Fault("resource_exhausted", "snapshot exceeds source frame limits")
            if rgb and len(rgb) != width * height * 3:
                raise Fault("internal", "RGB bytes do not match captured geometry")
            try:
                if jpeg_geometry(jpeg, MAX_FRAME_PIXELS) != (width, height):
                    raise FrameValidationError("JPEG dimensions do not match captured geometry")
            except FrameValidationError as error:
                raise Fault("internal", str(error)) from error
            if (not isinstance(capture.frame_id, str) or not capture.frame_id
                    or len(capture.frame_id.encode("utf-8")) > 256
                    or type(capture.frame_sequence) is not int or capture.frame_sequence < 1
                    or type(capture.timestamp_nanoseconds) is not int or capture.timestamp_nanoseconds < 0
                    or not isinstance(capture.timestamp_clock_domain, str)
                    or capture.timestamp_clock_domain not in {
                        "simulation", "system_realtime", "monotonic", "device", "unknown"}):
                raise Fault("internal", "capture provider returned invalid source identity")
            if ((width, height) != (self._description.width, self._description.height)
                    or capture.frame_id != self._description.frame_id):
                raise Fault("internal", "captured frame differs from frozen source geometry or frame identity")
            metadata = {
                "ok": True, "snapshotId": snapshot_id, "sourceId": self._description.source_id,
                "jpegBytes": len(jpeg), "rgbBytes": len(rgb), "width": width, "height": height,
                "frameId": capture.frame_id, "frameSequence": capture.frame_sequence,
                "pixelFormat": "rgb8", "jpegBackend": self._snapshot_backend,
                "jpegReadback": self._snapshot_readback if require_fresh else "latest-source-frame",
                "timestampNanoseconds": capture.timestamp_nanoseconds,
                "timestampClockDomain": capture.timestamp_clock_domain,
                "calibrationState": "unavailable",
            }
            context.check_cancelled()
            return multipart(metadata, jpeg, rgb)
        finally:
            self._capture_slots.release()
