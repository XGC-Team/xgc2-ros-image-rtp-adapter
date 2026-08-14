#!/usr/bin/env python3
"""Write one strict Media Edge source roster as an atomic private file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat


SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def write_source_roster(
    output: Path,
    source_id: str,
    rtp_port: int,
    control_socket: str,
) -> None:
    if not output.is_absolute():
        raise ValueError("output must be an absolute path")
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise ValueError("source ID must be a stable identifier")
    if not 1 <= rtp_port <= 65535:
        raise ValueError("RTP port must be between 1 and 65535")
    if not control_socket.startswith("/"):
        raise ValueError("control socket must be an absolute Unix path")

    document = {
        "sources": [
            {
                "controlSocket": control_socket,
                "id": source_id,
                "rtpListenAddress": f"127.0.0.1:{rtp_port}",
            }
        ]
    }
    payload = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")

    parent = output.parent
    try:
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise ValueError(
            "output parent must be an existing non-symbolic-link directory"
        ) from error

    temporary_name = ""
    try:
        try:
            target_mode = os.stat(
                output.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            ).st_mode
        except FileNotFoundError:
            target_mode = None
        if target_mode is not None and stat.S_ISLNK(target_mode):
            raise ValueError("output must not be a symbolic link")

        temporary_descriptor = -1
        for _ in range(32):
            temporary_name = f".{output.name}.{secrets.token_hex(12)}.tmp"
            try:
                temporary_descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                break
            except FileExistsError:
                continue
        if temporary_descriptor < 0:
            raise OSError("cannot allocate a unique roster temporary file")

        try:
            with os.fdopen(temporary_descriptor, "wb") as stream:
                temporary_descriptor = -1
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)

        os.replace(
            temporary_name,
            output.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = ""
        os.fsync(parent_descriptor)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write a canonical single-source Media Edge roster"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--rtp-port", type=int, required=True)
    parser.add_argument("--control-socket", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        write_source_roster(
            args.output,
            args.source_id,
            args.rtp_port,
            args.control_socket,
        )
    except (OSError, ValueError) as error:
        raise SystemExit(f"cannot write Media Edge source roster: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
