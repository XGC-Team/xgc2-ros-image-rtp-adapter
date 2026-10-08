"""Deployment must reject Python 3.8, missing SDK and unowned pip installs."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).parents[1] / ".xgc2/scripts/check_python_runtime.py"
SPEC = importlib.util.spec_from_file_location("camera_runtime_preflight", PATH)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def test_focal_python38_fails_before_importing_sdk(monkeypatch):
    monkeypatch.setattr(preflight.sys, "version_info", (3, 8, 10))
    with pytest.raises(RuntimeError, match="Focal/Noetic"):
        preflight.check_runtime()


def test_absent_distribution_cannot_pass_with_only_source_on_pythonpath(monkeypatch):
    def missing(_name):
        raise preflight.metadata.PackageNotFoundError("xgc2-xrpc")
    monkeypatch.setattr(preflight.metadata, "version", missing)
    with pytest.raises(preflight.metadata.PackageNotFoundError):
        preflight.check_runtime()


def test_sdk_without_debian_file_owner_cannot_enter_deb(tmp_path, monkeypatch):
    module = tmp_path / "sdk.py"
    module.write_text("# source-only SDK")
    def unowned(*_args, **_kwargs):
        raise preflight.subprocess.CalledProcessError(1, "dpkg-query")
    monkeypatch.setattr(preflight.subprocess, "run", unowned)
    with pytest.raises(preflight.subprocess.CalledProcessError):
        preflight.debian_dependency(SimpleNamespace(__file__=str(module)))


def test_resolved_package_owner_is_version_locked(tmp_path, monkeypatch):
    module = tmp_path / "sdk.py"
    module.write_text("# installed SDK")
    monkeypatch.setattr(preflight.subprocess, "run", lambda *_args, **_kwargs:
        SimpleNamespace(stdout="test-sdk: %s\n" % module))
    monkeypatch.setattr(preflight.subprocess, "check_output", lambda *_args, **_kwargs: "0.1.0-2")
    assert preflight.debian_dependency(SimpleNamespace(__file__=str(module))) == "test-sdk (= 0.1.0-2)"
