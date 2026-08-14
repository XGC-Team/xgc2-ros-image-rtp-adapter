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


DIGEST = "d" * 64
SOURCE_SHA = "a" * 40
MEDIA_EDGE_SHA = "e" * 40


def fake_deb_metadata(path: Path) -> dict[str, object]:
    return {
        "package": "ros-jazzy-xgc2-ros-image-rtp-adapter",
        "version": "0.4.0",
        "architecture": "amd64",
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def write_evidence(
    path: Path,
    *,
    prepare_action: str = "ci",
    dependency_mode: str = "locked-source",
    dependency_set_digest: str = DIGEST,
    source: str = f"https://github.com/lxk36/xgc2-media-edge.git@{MEDIA_EDGE_SHA}",
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "xgc2.dependency-evidence.v1",
                "prepareAction": prepare_action,
                "dependencySetDigest": dependency_set_digest,
                "dependencyMode": dependency_mode,
                "distribution": "noble",
                "architecture": "amd64",
                "dependencies": [
                    {
                        "package": "xgc2-media-edge",
                        "version": "0.6.0-1~noble",
                        "source": source,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def build_args(root: Path, evidence: Path) -> SimpleNamespace:
    return SimpleNamespace(
        deb_dir=str(root / "debs"),
        output_dir=str(root / "manifests"),
        product="xgc2-ros-image-rtp-adapter",
        product_version="0.4.0",
        distribution="noble",
        architecture="amd64",
        source_sha=SOURCE_SHA,
        ci_run_id="123",
        ci_workflow="ci",
        ci_workflow_ref="owner/repo/.github/workflows/ci.yml@refs/heads/jazzy",
        prepare_action="ci",
        dependency_set_digest=DIGEST,
        dependency_mode="locked-source",
        dependency_evidence=str(evidence),
    )


def verify_args(root: Path, evidence: Path) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_dir=str(root),
        deb_output_dir=str(root / "verified-debs"),
        manifest_output_dir=str(root / "verified-manifests"),
        product="xgc2-ros-image-rtp-adapter",
        product_version="0.4.0",
        distribution="noble",
        architecture="amd64",
        source_sha=SOURCE_SHA,
        ci_run_id="123",
        prepare_action="ci",
        dependency_set_digest=DIGEST,
        dependency_mode="locked-source",
        dependency_evidence=str(evidence),
    )


def test_v2_manifest_binds_exact_dependency_evidence(tmp_path: Path, monkeypatch) -> None:
    deb_dir = tmp_path / "debs"
    deb_dir.mkdir()
    (deb_dir / "adapter.deb").write_bytes(b"adapter")
    evidence = tmp_path / "evidence.json"
    write_evidence(evidence)
    monkeypatch.setattr(manifest_tool, "deb_metadata", fake_deb_metadata)

    manifest_tool.create_build(build_args(tmp_path, evidence))
    manifest_path = next((tmp_path / "manifests").glob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema"] == "xgc2.build-artifact.v2"
    assert manifest["prepareAction"] == "ci"
    assert manifest["dependencySetDigest"] == DIGEST
    assert manifest["dependencyMode"] == "locked-source"
    assert manifest["dependencies"] == [
        {
            "package": "xgc2-media-edge",
            "version": "0.6.0-1~noble",
            "source": f"https://github.com/lxk36/xgc2-media-edge.git@{MEDIA_EDGE_SHA}",
        }
    ]
    manifest_tool.verify_build(verify_args(tmp_path, evidence))


def test_verifier_rejects_v1_without_fallback(tmp_path: Path, monkeypatch) -> None:
    deb_dir = tmp_path / "debs"
    deb_dir.mkdir()
    (deb_dir / "adapter.deb").write_bytes(b"adapter")
    evidence = tmp_path / "evidence.json"
    write_evidence(evidence)
    (tmp_path / "old.json").write_text(
        json.dumps({"schema": "xgc2.build-artifact.v1"}), encoding="utf-8"
    )
    monkeypatch.setattr(manifest_tool, "deb_metadata", fake_deb_metadata)

    with pytest.raises(ValueError, match="no matching, valid build manifest"):
        manifest_tool.verify_build(verify_args(tmp_path, evidence))


@pytest.mark.parametrize(
    ("prepare_action", "dependency_mode", "source", "message"),
    [
        (
            "compatibility-verify",
            "locked-source",
            f"https://github.com/lxk36/xgc2-media-edge.git@{MEDIA_EDGE_SHA}",
            "compatibility-verify dependencyMode must be staging-apt",
        ),
        (
            "ci",
            "staging-apt",
            "https://apt.example/staging/release-1",
            "CI dependencyMode must be locked-source",
        ),
    ],
)
def test_evidence_rejects_action_mode_fallback(
    tmp_path: Path,
    prepare_action: str,
    dependency_mode: str,
    source: str,
    message: str,
) -> None:
    evidence = tmp_path / "evidence.json"
    write_evidence(
        evidence,
        prepare_action=prepare_action,
        dependency_mode=dependency_mode,
        source=source,
    )

    with pytest.raises(ValueError, match=message):
        manifest_tool.load_dependency_evidence(
            evidence,
            prepare_action=prepare_action,
            dependency_set_digest=DIGEST,
            dependency_mode=dependency_mode,
            distribution="noble",
            architecture="amd64",
        )


def test_staging_evidence_requires_exact_https_source(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    write_evidence(
        evidence,
        prepare_action="compatibility-verify",
        dependency_mode="staging-apt",
        source="http://apt.example/staging/release-1",
    )

    with pytest.raises(ValueError, match="HTTPS APT source"):
        manifest_tool.load_dependency_evidence(
            evidence,
            prepare_action="compatibility-verify",
            dependency_set_digest=DIGEST,
            dependency_mode="staging-apt",
            distribution="noble",
            architecture="amd64",
        )
