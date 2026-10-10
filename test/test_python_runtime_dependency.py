"""Verify the released Python runtime, SDK provenance and native capabilities."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).parents[1] / ".xgc2/scripts/check_python_runtime.py"
SPEC = importlib.util.spec_from_file_location("camera_runtime_preflight", PATH)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


@pytest.fixture
def installed_runtime(monkeypatch):
    sdk = SimpleNamespace(
        Runtime=SimpleNamespace(from_environment=lambda: None),
        Host=object, Client=object,
        Limits=SimpleNamespace(from_policy=lambda: None),
        multipart=object, resolve_policy=object,
    )
    versions = {
        "xgc2-xrpc": "0.1.0", "aiohttp": "3.10.11",
        "httpx": "0.28.1", "httpcore": "1.0.9",
    }
    provenance = {"archive_info": {"hashes": {"sha256":
        "8e505ab2366eed198dcd4343e758fed5b7936990b2a72ba635d73d81b195187c"}}}
    monkeypatch.setattr(preflight.metadata, "version", versions.__getitem__)
    monkeypatch.setattr(preflight.metadata, "distribution", lambda _name:
        SimpleNamespace(read_text=lambda _path: json.dumps(provenance)))
    monkeypatch.setattr(preflight.importlib, "import_module", lambda _name: sdk)
    return sdk, provenance


def test_python38_is_supported_but_older_python_is_rejected(monkeypatch, installed_runtime):
    monkeypatch.setattr(preflight.sys, "version_info", (3, 7, 17))
    with pytest.raises(RuntimeError, match="Python >=3.8"):
        preflight.check_runtime()
    monkeypatch.setattr(preflight.sys, "version_info", (3, 8, 10))
    sdk, versions = preflight.check_runtime()
    assert sdk is installed_runtime[0]
    assert versions["xgc2-xrpc"] == "0.1.0"


def test_absent_distribution_cannot_pass_with_only_source_on_pythonpath(monkeypatch):
    def missing(_name):
        raise preflight.metadata.PackageNotFoundError("xgc2-xrpc")
    monkeypatch.setattr(preflight.metadata, "version", missing)
    with pytest.raises(preflight.metadata.PackageNotFoundError):
        preflight.check_runtime()


def test_sdk_with_different_wheel_provenance_is_rejected(installed_runtime):
    installed_runtime[1]["archive_info"]["hashes"]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="released image-owned wheel"):
        preflight.check_runtime()


def test_sdk_without_native_runtime_capability_is_rejected(installed_runtime):
    installed_runtime[0].Runtime = object
    with pytest.raises(RuntimeError, match="startup policy snapshots"):
        preflight.check_runtime()
