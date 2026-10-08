#!/usr/bin/env python3
"""Verify the actual installed SDK, interpreter and Debian dependency owner."""
import argparse
import importlib
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import subprocess
import sys


def check_runtime():
    if sys.version_info < (3, 10):
        raise RuntimeError("Python >=3.10 is required; Focal/Noetic's default Python 3.8 is unsupported")
    versions = {name: metadata.version(name) for name in ("xgc2-xrpc", "aiohttp", "httpx", "httpcore")}
    expected = {"xgc2-xrpc": "0.1.0", "aiohttp": "3.14.4", "httpx": "0.28.1", "httpcore": "1.0.9"}
    if versions != expected:
        raise RuntimeError("installed XRPC dependency versions differ: %s" % versions)
    sdk = importlib.import_module("xgc2_xrpc")
    if any(not hasattr(sdk, name) for name in ("Runtime", "Host", "Client", "Limits", "multipart", "resolve_policy")):
        raise RuntimeError("installed XRPC SDK does not expose the native Runtime/Host API")
    if not hasattr(sdk.Runtime, "from_environment") or not hasattr(sdk.Limits, "from_policy"):
        raise RuntimeError("installed SDK does not expose startup policy snapshots")
    return sdk, versions


def source_digest(sdk):
    root = Path(sdk.__file__).resolve(strict=True).parent
    digest = hashlib.sha256()
    paths = sorted(path for path in root.iterdir() if path.suffix in (".py", ".json"))
    for path in paths:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def debian_dependency(sdk):
    # Derive the real package owner instead of inventing an unpublished apt name
    # or silently packaging a dependency installed only in the build workspace.
    path = str(Path(sdk.__file__).resolve(strict=True))
    result = subprocess.run(["dpkg-query", "-S", path], check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    records = result.stdout.strip().splitlines()
    if len(records) != 1 or ": " not in records[0]:
        raise RuntimeError("SDK must have exactly one Debian file owner")
    package, owned_path = records[0].split(": ", 1)
    if owned_path != path or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?", package):
        raise RuntimeError("SDK Debian ownership does not match its imported path")
    version = subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", package], text=True)
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", version):
        raise RuntimeError("invalid SDK Debian package version")
    return "%s (= %s)" % (package, version)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debian-dependency", action="store_true")
    args = parser.parse_args()
    try:
        sdk, versions = check_runtime()
        print(debian_dependency(sdk) if args.debian_dependency else json.dumps({
            "python": sys.version.split()[0], "dependencies": versions,
            "sdk_source_sha256": source_digest(sdk), "publication": "candidate"}, sort_keys=True))
        return 0
    except (RuntimeError, ImportError, OSError, subprocess.SubprocessError) as error:
        print("XRPC runtime preflight failed: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
