#!/usr/bin/env python3
"""Wait until media-edge control describe succeeds with expected fields."""

from __future__ import annotations

import argparse
import json
from xgc2_xrpc import Client, Runtime
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--rtp-port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    last_error = "not started"
    with Runtime.from_environment() as runtime, Client(args.socket, runtime=runtime) as client:
        while time.monotonic() < deadline:
            try:
                client.instance_id = ""
                discovery = client.json("/v1/describe", method="GET", timeout=max(0.001, deadline - time.monotonic()))
                client.instance_id = discovery["service_ref"]["instance_id"]
                desc = client.json("/v1/media/sources/" + args.source_id + "/describe", method="GET",
                                   timeout=max(0.001, deadline - time.monotonic()))
                if not desc.get("ok"):
                    last_error = str(desc)
                    time.sleep(0.5)
                    continue
                if desc.get("sourceId") != args.source_id:
                    last_error = "sourceId mismatch: %s" % desc
                    time.sleep(0.5)
                    continue
                if desc.get("codec") != "H264":
                    last_error = "codec mismatch: %s" % desc
                    time.sleep(0.5)
                    continue
                if int(desc.get("rtpPort", -1)) != args.rtp_port:
                    last_error = "rtpPort mismatch: %s" % desc
                    time.sleep(0.5)
                    continue
                print(json.dumps(desc, sort_keys=True))
                return 0
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                time.sleep(0.5)
    print(last_error, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
