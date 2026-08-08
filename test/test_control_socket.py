import json
import os
import socket
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
        snaps = {"jpeg": b"\xff\xd8fakejpeg\xff\xd9"}

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
            on_snapshot=lambda: snaps["jpeg"],
        )
        server.start()
        try:
            desc = _request(path, {"operation": "describe"})
            self.assertTrue(desc["ok"])
            self.assertEqual(desc["sourceId"], "odin1")
            self.assertEqual(desc["codec"], "H264")
            self.assertEqual(desc["rtpPayloadType"], 96)
            self.assertEqual(desc["rtpPort"], 5004)
            self.assertIn("set-active", desc["capabilities"])

            resp = _request(path, {"operation": "set-active", "active": False})
            self.assertTrue(resp["ok"])
            self.assertFalse(active["value"])

            resp = _request(path, {"operation": "request-keyframe"})
            self.assertTrue(resp["ok"])
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
