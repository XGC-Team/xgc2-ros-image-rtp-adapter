import json
import os
import socket
import stat
import tempfile
import time
import unittest
from xgc2_xrpc import Runtime

from ros_image_rtp_adapter.control_socket import SourceControlServer, SourceDescription, SnapshotCapture


def _request(path: str, payload: dict) -> dict:
    import email.parser
    import email.policy
    from xgc2_xrpc import Client, Fault
    payload = dict(payload)
    operation = payload.pop("operation")
    try:
        with Runtime() as runtime, Client(path,runtime=runtime) as client:
            discovery = client.json("/v1/describe", method="GET")
            reference = discovery["service_ref"]
            client.instance_id = reference["instance_id"]
            method = "GET" if operation in ("describe", "status") else "POST"
            response = client.call("/v1/media/sources/" + discovery["sources"][0] + "/" + operation,
                                   None if method == "GET" else payload, method=method)
            if response.content_type.startswith("multipart/mixed"):
                message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                    ("Content-Type: " + response.content_type + "\r\n\r\n").encode() + response.body)
                parts = list(message.iter_parts())
                metadata = json.loads(parts[0].get_payload(decode=True))
                assert len(parts[1].get_payload(decode=True)) == metadata["jpegBytes"]
                return metadata
            return json.loads(response.body)
    except Fault as error:
        return {"ok": False, "error": str(error), "code": error.code}


class ControlSocketTest(unittest.TestCase):
    def setUp(self):
        self.runtime=Runtime()
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.runtime.close)

    def make_server(self,*args,**kwargs):
        return SourceControlServer(*args,runtime=self.runtime,**kwargs)

    def test_describe_set_active_and_snapshot(self):
        fd, path = tempfile.mkstemp(prefix="xgc2-image-rtp-", suffix=".sock", dir=self.directory.name)
        os.close(fd)
        os.unlink(path)

        active = {"value": True}
        from io import BytesIO
        from PIL import Image
        image = BytesIO()
        Image.new("RGB", (640, 360)).save(image, format="JPEG")
        snaps = {"jpeg": image.getvalue(), "include_rgb": []}

        server = self.make_server(
            path,
            SourceDescription(
                source_id="odin1",
                rtp_host="127.0.0.1",
                rtp_port=5004,
                width=640,
                height=360,
                fps=10.0,
                frame_id="camera_optical",
                keyframe_request_supported=True,
            ),
            on_set_active=lambda v: active.__setitem__("value", v),
            on_request_keyframe=lambda: None,
            on_snapshot=lambda include_rgb, require_fresh: (
                snaps["include_rgb"].append((include_rgb, require_fresh))
                or SnapshotCapture(snaps["jpeg"], width=640, height=360,
                                   frame_id="camera_optical", frame_sequence=1)
            ),
        )
        server.start()
        try:
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertFalse(server.active)
            desc = _request(path, {"operation": "describe"})
            self.assertTrue(desc["ok"])
            self.assertEqual(desc["sourceId"], "odin1")
            self.assertEqual(desc["codec"], "H264")
            self.assertEqual(desc["rtpPayloadType"], 96)
            self.assertEqual(desc["rtpPort"], 5004)
            self.assertEqual(desc["snapshotJpegPolicy"], "source")
            self.assertEqual(desc["snapshotJpegBackend"], "source-jpeg-passthrough")
            self.assertEqual(desc["snapshotJpegHardwareState"], "source-owned")
            self.assertIn("start", desc["capabilities"])
            self.assertIn("fresh-snapshot", desc["capabilities"])

            resp = _request(path, {"operation": "stop"})
            self.assertTrue(resp["ok"])
            self.assertFalse(active["value"])

            resp = _request(path, {"operation": "request-keyframe"})
            self.assertTrue(resp["ok"])

            resp = _request(path, {
                "operation": "capture", "snapshotId": "jpeg-only", "includeRgb": False,
                "requireFresh": True,
            })
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["jpegBytes"], len(snaps["jpeg"]))
            self.assertEqual(resp["rgbBytes"], 0)
            self.assertEqual(resp["jpegBackend"], "source-jpeg-passthrough")
            self.assertEqual(resp["jpegReadback"], "latest-source-frame")
            self.assertEqual(snaps["include_rgb"], [(False, True)])

            resp = _request(path, {"operation": "start", "active": "yes"})
            self.assertFalse(resp["ok"])
            self.assertIn("unknown", resp["error"])
        finally:
            server.stop()

    def test_start_refuses_existing_path_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.sock")
            with open(path, "wb") as existing:
                existing.write(b"owned by another process")
            server = self.make_server(
                path,
                SourceDescription("camera", "127.0.0.1", 5004, 640, 360, 10, "camera"),
            )
            with self.assertRaises(FileExistsError):
                server.start()
            with open(path, "rb") as existing:
                self.assertEqual(existing.read(), b"owned by another process")

    def test_stop_wakes_the_blocked_accept_without_waiting_for_its_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.sock")
            server = self.make_server(
                path,
                SourceDescription("camera", "127.0.0.1", 5004, 640, 360, 10, "camera"),
            )
            server.start()
            started = time.monotonic()
            server.stop()
            self.assertLess(time.monotonic() - started, 0.2)

    def test_start_recovers_stale_socket_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.sock")
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(path)
            stale.close()

            server = self.make_server(
                path,
                SourceDescription("camera", "127.0.0.1", 5004, 640, 360, 10, "camera"),
            )
            server.start()
            try:
                response = _request(path, {"operation": "describe"})
                self.assertTrue(response["ok"])
                self.assertEqual(response["sourceId"], "camera")
            finally:
                server.stop()
            self.assertFalse(os.path.lexists(path))

    def test_start_preserves_socket_with_active_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(4)
            original = os.stat(path, follow_symlinks=False)

            server = self.make_server(
                path,
                SourceDescription("camera", "127.0.0.1", 5004, 640, 360, 10, "camera"),
            )
            try:
                with self.assertRaises(FileExistsError):
                    server.start()
                current = os.stat(path, follow_symlinks=False)
                self.assertEqual(
                    (current.st_dev, current.st_ino),
                    (original.st_dev, original.st_ino),
                )

                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(1.0)
                client.connect(path)
                client.close()
            finally:
                listener.close()
                os.unlink(path)

    def test_start_refuses_symlink_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            real_parent = os.path.join(directory, "real")
            linked_parent = os.path.join(directory, "linked")
            os.mkdir(real_parent)
            os.symlink(real_parent, linked_parent)
            path = os.path.join(linked_parent, "source.sock")
            server = self.make_server(
                path,
                SourceDescription("camera", "127.0.0.1", 5004, 640, 360, 10, "camera"),
            )
            with self.assertRaises(OSError):
                server.start()
            self.assertFalse(os.path.lexists(os.path.join(real_parent, "source.sock")))

    def test_stop_does_not_delete_replacement_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.sock")
            server = self.make_server(
                path,
                SourceDescription("camera", "127.0.0.1", 5004, 640, 360, 10, "camera"),
            )
            server.start()
            os.unlink(path)
            with open(path, "wb") as replacement:
                replacement.write(b"replacement")
            server.stop()
            with open(path, "rb") as replacement:
                self.assertEqual(replacement.read(), b"replacement")

    def test_failed_activation_keeps_server_available_and_inactive(self):
        fd, path = tempfile.mkstemp(prefix="xgc2-image-rtp-", suffix=".sock", dir=self.directory.name)
        os.close(fd)
        os.unlink(path)

        def fail_activation(_active):
            raise RuntimeError("encoder unavailable")

        server = self.make_server(
            path,
            SourceDescription(
                source_id="camera",
                rtp_host="127.0.0.1",
                rtp_port=5004,
                width=640,
                height=360,
                fps=10.0,
                frame_id="camera_optical",
            ),
            on_set_active=fail_activation,
        )
        server.start()
        try:
            response = _request(path, {"operation": "start"})
            self.assertFalse(response["ok"])
            self.assertIn("encoder unavailable", response["error"])
            self.assertFalse(server.active)
            self.assertTrue(_request(path, {"operation": "describe"})["ok"])
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
