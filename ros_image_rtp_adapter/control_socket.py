"""Newline-delimited JSON Unix control socket for xgc2-media-edge source contract."""

from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional


PROTOCOL_VERSION = 1


@dataclass
class SourceDescription:
    source_id: str
    rtp_host: str
    rtp_port: int
    width: int
    height: int
    fps: float
    frame_id: str
    capabilities: tuple = ("set-active", "request-keyframe", "snapshot")

    def as_dict(self) -> Dict:
        return {
            "ok": True,
            "protocolVersion": PROTOCOL_VERSION,
            "sourceId": self.source_id,
            "codec": "H264",
            "rtpPayloadType": 96,
            "rtpClockRate": 90000,
            "rtpHost": self.rtp_host,
            "rtpPort": self.rtp_port,
            "width": int(self.width),
            "height": int(self.height),
            "fps": float(self.fps),
            "frameId": self.frame_id,
            "capabilities": list(self.capabilities),
            "timestampClockDomain": "system_realtime",
        }


class SourceControlServer:
    """Serve media-edge control operations on a Unix domain socket."""

    def __init__(
        self,
        path: str,
        description: SourceDescription,
        *,
        on_set_active: Optional[Callable[[bool], None]] = None,
        on_request_keyframe: Optional[Callable[[], None]] = None,
        on_snapshot: Optional[Callable[[], Optional[bytes]]] = None,
    ) -> None:
        self._path = path
        self._description = description
        self._on_set_active = on_set_active
        self._on_request_keyframe = on_request_keyframe
        self._on_snapshot = on_snapshot
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[socket.socket] = None
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._thread is not None:
            return
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._path)
        os.chmod(self._path, 0o666)
        server.listen(16)
        server.settimeout(0.5)
        self._server = server
        self._thread = threading.Thread(target=self._serve_loop, name="source-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

    def _serve_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            try:
                self._handle_connection(conn)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buffer.extend(chunk)
            if len(buffer) > 1_000_000:
                self._write_json(conn, {"ok": False, "error": "control request too large"})
                return
        line, _ = bytes(buffer).split(b"\n", 1)
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._write_json(conn, {"ok": False, "error": f"invalid json: {exc}"})
            return
        operation = str(request.get("operation", "")).strip()
        if operation == "describe":
            self._write_json(conn, self._description.as_dict())
            return
        if operation == "set-active":
            active = bool(request.get("active", True))
            self._active = active
            if self._on_set_active is not None:
                self._on_set_active(active)
            self._write_json(conn, {"ok": True, "active": active})
            return
        if operation == "request-keyframe":
            if self._on_request_keyframe is not None:
                self._on_request_keyframe()
            self._write_json(conn, {"ok": True})
            return
        if operation == "snapshot":
            jpeg = b""
            if self._on_snapshot is not None:
                jpeg = self._on_snapshot() or b""
            header = {
                "ok": True,
                "snapshotId": str(request.get("snapshotId", "")),
                "jpegBytes": len(jpeg),
                "rgbBytes": 0,
                "width": self._description.width,
                "height": self._description.height,
                "frameId": self._description.frame_id,
                "timestampClockDomain": "system_realtime",
            }
            conn.sendall(json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + jpeg)
            return
        self._write_json(conn, {"ok": False, "error": f"unsupported operation: {operation}"})

    @staticmethod
    def _write_json(conn: socket.socket, payload: Dict) -> None:
        conn.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
