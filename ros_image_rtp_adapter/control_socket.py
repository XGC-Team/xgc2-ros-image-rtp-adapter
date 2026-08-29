"""Newline-delimited JSON Unix control socket for xgc2-media-edge source contract."""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple


PROTOCOL_VERSION = 1
_SOCKET_CREATION_LOCK = threading.Lock()


@dataclass
class SourceDescription:
    source_id: str
    rtp_host: str
    rtp_port: int
    width: int
    height: int
    fps: float
    frame_id: str
    capabilities: tuple = (
        "set-active", "request-keyframe", "snapshot", "fresh-snapshot",
    )

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
        on_snapshot: Optional[Callable[[bool, bool], Optional[bytes]]] = None,
    ) -> None:
        self._path = path
        self._description = description
        self._on_set_active = on_set_active
        self._on_request_keyframe = on_request_keyframe
        self._on_snapshot = on_snapshot
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[socket.socket] = None
        self._parent_fd: Optional[int] = None
        self._socket_name = ""
        self._socket_identity: Optional[Tuple[int, int]] = None
        # Capture sources are demand-driven.  Edge explicitly activates them
        # for a viewer, recording, or snapshot transaction.
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._thread is not None:
            return
        if not os.path.isabs(self._path):
            raise ValueError("control socket path must be absolute")
        if len(os.fsencode(self._path)) > 107:
            raise ValueError("control socket path exceeds the Linux Unix socket limit")
        parent, socket_name = os.path.split(self._path)
        if not socket_name or socket_name in {".", ".."}:
            raise ValueError("control socket path must name one socket entry")
        parent_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            self._recover_stale_socket(parent_fd, socket_name)
        except Exception:
            os.close(parent_fd)
            raise

        server: Optional[socket.socket] = None
        socket_identity: Optional[Tuple[int, int]] = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Bind through the open directory descriptor so a concurrent parent
            # path replacement cannot redirect creation outside the inspected
            # run-owned directory.
            # Unix socket mode is selected at bind time. Serialize the brief
            # process-wide umask change so this module never creates a
            # world-readable control endpoint.
            with _SOCKET_CREATION_LOCK:
                previous_umask = os.umask(0o177)
                try:
                    server.bind("/proc/self/fd/%d/%s" % (parent_fd, socket_name))
                finally:
                    os.umask(previous_umask)
            socket_stat = os.stat(
                socket_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise RuntimeError("bound control endpoint is not a Unix socket")
            if stat.S_IMODE(socket_stat.st_mode) != 0o600:
                raise RuntimeError("control socket permissions are not private")
            server.listen(16)
            server.settimeout(0.5)
        except Exception:
            if server is not None:
                server.close()
            self._unlink_if_owned(parent_fd, socket_name, socket_identity)
            os.close(parent_fd)
            raise

        self._stop.clear()
        self._server = server
        self._parent_fd = parent_fd
        self._socket_name = socket_name
        self._socket_identity = socket_identity
        self._thread = threading.Thread(
            target=self._serve_loop,
            name="source-control",
            daemon=True,
        )
        self._thread.start()

    def _recover_stale_socket(self, parent_fd: int, socket_name: str) -> None:
        try:
            existing = os.stat(
                socket_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return

        if not stat.S_ISSOCK(existing.st_mode):
            raise FileExistsError("control socket path already exists: %s" % self._path)

        existing_identity = (existing.st_dev, existing.st_ino)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect("/proc/self/fd/%d/%s" % (parent_fd, socket_name))
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return
            if exc.errno != errno.ECONNREFUSED:
                raise FileExistsError(
                    "control socket path is occupied and could not be proven stale: %s"
                    % self._path
                ) from exc
        else:
            raise FileExistsError(
                "control socket path already has an active listener: %s" % self._path
            )
        finally:
            probe.close()

        # A refused connection proves there is no listener for this pathname.
        # Remove only the exact socket inode that was probed; if another owner
        # replaced the entry in the meantime, the subsequent bind fails closed.
        self._unlink_if_owned(parent_fd, socket_name, existing_identity)

    def stop(self) -> None:
        self._stop.set()
        server = self._server
        if server is not None:
            wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            wake.settimeout(0.1)
            try:
                # A local connection wakes accept portably; closing a listening
                # fd from another thread does not wake accept on every Linux
                # runtime. The empty connection returns immediately in the
                # handler because the stop-side closes it without a request.
                wake.connect(self._path)
            except OSError:
                pass
            finally:
                wake.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if self._parent_fd is not None:
            self._unlink_if_owned(
                self._parent_fd,
                self._socket_name,
                self._socket_identity,
            )
            os.close(self._parent_fd)
            self._parent_fd = None
        self._socket_name = ""
        self._socket_identity = None

    @staticmethod
    def _unlink_if_owned(
        parent_fd: int,
        socket_name: str,
        expected_identity: Optional[Tuple[int, int]],
    ) -> None:
        if not socket_name or expected_identity is None:
            return
        try:
            current = os.stat(
                socket_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(current.st_mode):
            return
        if (current.st_dev, current.st_ino) != expected_identity:
            return
        os.unlink(socket_name, dir_fd=parent_fd)

    def _serve_loop(self) -> None:
        server = self._server
        assert server is not None
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
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
            if "active" not in request or not isinstance(request["active"], bool):
                self._write_json(
                    conn,
                    {"ok": False, "error": "active must be a boolean"},
                )
                return
            active = request["active"]
            previous = self._active
            try:
                if self._on_set_active is not None:
                    self._on_set_active(active)
            except Exception as exc:  # The source must stay controllable.
                self._active = previous
                self._write_json(
                    conn,
                    {"ok": False, "error": f"set-active failed: {exc}"},
                )
                return
            self._active = active
            self._write_json(conn, {"ok": True, "active": active})
            return
        if operation == "request-keyframe":
            if self._on_request_keyframe is not None:
                self._on_request_keyframe()
            self._write_json(conn, {"ok": True})
            return
        if operation == "snapshot":
            include_rgb = request.get("includeRgb", True)
            if not isinstance(include_rgb, bool):
                self._write_json(conn, {"ok": False, "error": "includeRgb must be a boolean"})
                return
            require_fresh = request.get("requireFresh", False)
            if not isinstance(require_fresh, bool):
                self._write_json(conn, {"ok": False, "error": "requireFresh must be a boolean"})
                return
            jpeg = b""
            rgb = b""
            if self._on_snapshot is not None:
                payload = self._on_snapshot(include_rgb, require_fresh)
                if isinstance(payload, tuple) and len(payload) == 2:
                    jpeg = payload[0] or b""
                    rgb = payload[1] or b""
                else:
                    jpeg = payload or b""
            if not include_rgb:
                rgb = b""
            width = int(self._description.width)
            height = int(self._description.height)
            header = {
                "ok": True,
                "snapshotId": str(request.get("snapshotId", "")),
                "jpegBytes": len(jpeg),
                "rgbBytes": len(rgb),
                "width": width,
                "height": height,
                "frameId": self._description.frame_id,
                "pixelFormat": "rgb8",
                "timestampNanoseconds": time.time_ns(),
                "timestampClockDomain": "system_realtime",
                "cameraMatrix": [float(width), 0.0, width / 2.0, 0.0, float(width), height / 2.0, 0.0, 0.0, 1.0],
                "distortion": [0.0, 0.0, 0.0, 0.0, 0.0],
            }
            conn.sendall(json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + jpeg + rgb)
            return
        self._write_json(conn, {"ok": False, "error": f"unsupported operation: {operation}"})

    @staticmethod
    def _write_json(conn: socket.socket, payload: Dict) -> None:
        conn.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
