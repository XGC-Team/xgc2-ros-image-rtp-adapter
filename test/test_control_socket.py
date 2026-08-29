import json
import os
import socket
import stat
import tempfile
import time
import unittest

from ros_image_rtp_adapter.control_socket import SourceControlServer, SourceDescription


def _request(path: str, payload: dict) -> dict:
    deadline = time.time() + 5.0
    last = None
    while time.time() < deadline:
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(2.0)
            conn.connect(path)
            conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            conn.close()
            line = data.split(b"\n", 1)[0]
            return json.loads(line.decode("utf-8"))
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    raise AssertionError("control socket not ready: %s" % last)


class ControlSocketTest(unittest.TestCase):
    def test_describe_set_active_and_snapshot(self):
        fd, path = tempfile.mkstemp(prefix="xgc2-image-rtp-", suffix=".sock")
        os.close(fd)
        os.unlink(path)

        active = {"value": True}
        snaps = {"jpeg": b"\xff\xd8fakejpeg\xff\xd9", "include_rgb": []}

        server = SourceControlServer(
            path,
            SourceDescription(
                source_id="odin1",
                rtp_host="127.0.0.1",
                rtp_port=5004,
                width=640,
                height=360,
                fps=10.0,
                frame_id="camera_optical",
            ),
            on_set_active=lambda v: active.__setitem__("value", v),
            on_request_keyframe=lambda: None,
            on_snapshot=lambda include_rgb, require_fresh: (
                snaps["include_rgb"].append((include_rgb, require_fresh)) or snaps["jpeg"]
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
            self.assertIn("set-active", desc["capabilities"])
            self.assertIn("fresh-snapshot", desc["capabilities"])

            resp = _request(path, {"operation": "set-active", "active": False})
            self.assertTrue(resp["ok"])
            self.assertFalse(active["value"])

            resp = _request(path, {"operation": "request-keyframe"})
            self.assertTrue(resp["ok"])

            resp = _request(path, {
                "operation": "snapshot", "snapshotId": "jpeg-only", "includeRgb": False,
                "requireFresh": True,
            })
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["jpegBytes"], len(snaps["jpeg"]))
            self.assertEqual(resp["rgbBytes"], 0)
            self.assertEqual(resp["jpegBackend"], "source-jpeg-passthrough")
            self.assertEqual(resp["jpegReadback"], "latest-source-frame")
            self.assertEqual(snaps["include_rgb"], [(False, True)])

            resp = _request(path, {"operation": "set-active", "active": "yes"})
            self.assertFalse(resp["ok"])
            self.assertIn("boolean", resp["error"])
        finally:
            server.stop()

    def test_start_refuses_existing_path_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.sock")
            with open(path, "wb") as existing:
                existing.write(b"owned by another process")
            server = SourceControlServer(
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
            server = SourceControlServer(
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

            server = SourceControlServer(
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

            server = SourceControlServer(
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
            server = SourceControlServer(
                path,
                SourceDescription("camera", "127.0.0.1", 5004, 640, 360, 10, "camera"),
            )
            with self.assertRaises(OSError):
                server.start()
            self.assertFalse(os.path.lexists(os.path.join(real_parent, "source.sock")))

    def test_stop_does_not_delete_replacement_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.sock")
            server = SourceControlServer(
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
        fd, path = tempfile.mkstemp(prefix="xgc2-image-rtp-", suffix=".sock")
        os.close(fd)
        os.unlink(path)

        def fail_activation(_active):
            raise RuntimeError("encoder unavailable")

        server = SourceControlServer(
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
            response = _request(path, {"operation": "set-active", "active": True})
            self.assertFalse(response["ok"])
            self.assertIn("encoder unavailable", response["error"])
            self.assertFalse(server.active)
            self.assertTrue(_request(path, {"operation": "describe"})["ok"])
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
