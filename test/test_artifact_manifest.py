from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / ".xgc2" / "scripts" / "xgc2_artifact_manifest.py"
SPEC = importlib.util.spec_from_file_location("xgc2_artifact_manifest", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
manifest_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest_tool)


SOURCE_SHA = "a" * 40


def fake_deb_metadata(path: Path) -> dict[str, object]:
    return {
        "package": "ros-jazzy-xgc2-ros-image-rtp-adapter",
        "version": "0.4.3",
        "architecture": "amd64",
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def build_args(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        deb_dir=str(root / "debs"),
        output_dir=str(root / "manifests"),
        product="xgc2-ros-image-rtp-adapter",
        product_version="0.4.3",
        distribution="noble",
        architecture="amd64",
        source_sha=SOURCE_SHA,
        ci_run_id="123",
        ci_workflow="ci",
        ci_workflow_ref="owner/repo/.github/workflows/ci.yml@refs/heads/jazzy",
    )


def verify_args(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_dir=str(root),
        deb_output_dir=str(root / "verified-debs"),
        manifest_output_dir=str(root / "verified-manifests"),
        product="xgc2-ros-image-rtp-adapter",
        product_version="0.4.3",
        distribution="noble",
        architecture="amd64",
        source_sha=SOURCE_SHA,
        ci_run_id="123",
    )


def test_v1_manifest_uses_exact_central_fields(tmp_path: Path, monkeypatch) -> None:
    deb_dir = tmp_path / "debs"
    deb_dir.mkdir()
    (deb_dir / "adapter.deb").write_bytes(b"adapter")
    monkeypatch.setattr(manifest_tool, "deb_metadata", fake_deb_metadata)

    manifest_tool.create_build(build_args(tmp_path))
    manifest_path = next((tmp_path / "manifests").glob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema"] == "xgc2.build-artifact.v1"
    assert set(manifest) == manifest_tool.BUILD_FIELDS
    assert manifest["created_at"].endswith("Z")
    for removed in ("prepareAction", "dependencySetDigest", "dependencyMode", "dependencies"):
        assert removed not in manifest
    manifest_tool.verify_build(verify_args(tmp_path))


def test_verifier_rejects_incomplete_v1_manifest(tmp_path: Path, monkeypatch) -> None:
    deb_dir = tmp_path / "debs"
    deb_dir.mkdir()
    (deb_dir / "adapter.deb").write_bytes(b"adapter")
    (tmp_path / "old.json").write_text(
        json.dumps({"schema": "xgc2.build-artifact.v1"}), encoding="utf-8"
    )
    monkeypatch.setattr(manifest_tool, "deb_metadata", fake_deb_metadata)

    with pytest.raises(ValueError, match="build manifest fields are not exact"):
        manifest_tool.verify_build(verify_args(tmp_path))
