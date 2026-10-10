#!/usr/bin/env python3
"""Verify the released image-owned SDK and its actual runtime capabilities."""
import importlib
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys


def check_runtime():
    if sys.version_info < (3, 8):
        raise RuntimeError("Python >=3.8 is required")
    versions = {name: metadata.version(name) for name in ("xgc2-xrpc", "aiohttp", "httpx", "httpcore")}
    expected = {"xgc2-xrpc": "0.1.0", "aiohttp": "3.10.11", "httpx": "0.28.1", "httpcore": "1.0.9"}
    if versions != expected:
        raise RuntimeError("installed XRPC dependency versions differ: %s" % versions)
    sdk = importlib.import_module("xgc2_xrpc")
    if any(not hasattr(sdk, name) for name in ("Runtime", "Host", "Client", "Limits", "multipart", "resolve_policy")):
        raise RuntimeError("installed XRPC SDK does not expose the native Runtime/Host API")
    if not hasattr(sdk.Runtime, "from_environment") or not hasattr(sdk.Limits, "from_policy"):
        raise RuntimeError("installed SDK does not expose startup policy snapshots")
    provenance = json.loads(metadata.distribution("xgc2-xrpc").read_text("direct_url.json") or "{}")
    wheel_digest = provenance.get("archive_info", {}).get("hashes", {}).get("sha256")
    if wheel_digest != "8e505ab2366eed198dcd4343e758fed5b7936990b2a72ba635d73d81b195187c":
        raise RuntimeError("installed XRPC SDK differs from the released image-owned wheel")
    return sdk, versions


def source_digest(sdk):
    root = Path(sdk.__file__).resolve(strict=True).parent
    digest = hashlib.sha256()
    paths = sorted(path for path in root.iterdir() if path.suffix in (".py", ".json"))
    for path in paths:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    try:
        sdk, versions = check_runtime()
        print(json.dumps({
            "python": sys.version.split()[0], "dependencies": versions,
            "sdk_source_sha256": source_digest(sdk), "publication": "v0.1.0-1"}, sort_keys=True))
        return 0
    except (RuntimeError, ImportError, OSError, ValueError) as error:
        print("XRPC runtime preflight failed: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
