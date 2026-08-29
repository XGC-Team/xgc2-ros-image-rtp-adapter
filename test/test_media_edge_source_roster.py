import importlib.util
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "write_media_edge_source_roster.py"
)
LAB_SCRIPT = SCRIPT.with_name("lab_video_preview.sh")
INTEGRATION_SCRIPT = SCRIPT.with_name("integration_media_edge.sh")
DOCKER_BUILD_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".xgc2"
    / "scripts"
    / "build_debs_in_docker.sh"
)
INTEGRATION_LOCK = DOCKER_BUILD_SCRIPT.parents[1] / "integration-lock.json"
READ_INTEGRATION_LOCK = DOCKER_BUILD_SCRIPT.with_name("read_integration_lock.py")

SPEC = importlib.util.spec_from_file_location("media_edge_roster_writer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
WRITER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRITER)


def run_writer(output, *, source_id="camera", rtp_port=5004, check=True):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--source-id",
            source_id,
            "--rtp-port",
            str(rtp_port),
            "--control-socket",
            "/run/xgc2/camera.sock",
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def temporary_files(directory, output_name):
    return list(directory.glob(f".{output_name}.*.tmp"))


def test_writer_creates_exact_private_canonical_roster(tmp_path):
    output = tmp_path / "sources.json"

    run_writer(output)

    assert output.read_bytes() == (
        b'{"sources":[{"controlSocket":"/run/xgc2/camera.sock",'
        b'"id":"camera","rtpListenAddress":"127.0.0.1:5004"}]}\n'
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text(encoding="utf-8"))["sources"][0]["id"] == "camera"
    assert temporary_files(tmp_path, output.name) == []


def test_writer_atomically_replaces_a_previous_roster(tmp_path):
    output = tmp_path / "sources.json"
    run_writer(output)

    run_writer(output, source_id="camera_two", rtp_port=5006)

    assert output.read_bytes() == (
        b'{"sources":[{"controlSocket":"/run/xgc2/camera.sock",'
        b'"id":"camera_two","rtpListenAddress":"127.0.0.1:5006"}]}\n'
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert temporary_files(tmp_path, output.name) == []


def test_writer_rejects_a_symbolic_link_without_touching_its_target(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("owned elsewhere\n", encoding="utf-8")
    output = tmp_path / "sources.json"
    output.symlink_to(target)

    result = run_writer(output, check=False)

    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert output.is_symlink()
    assert target.read_text(encoding="utf-8") == "owned elsewhere\n"
    assert temporary_files(tmp_path, output.name) == []


def test_writer_rejects_a_symbolic_parent_directory(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    result = run_writer(linked_parent / "sources.json", check=False)

    assert result.returncode != 0
    assert "non-symbolic-link directory" in result.stderr
    assert list(real_parent.iterdir()) == []


def test_writer_preserves_previous_roster_when_atomic_replace_fails(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "sources.json"
    output.write_text("previous roster\n", encoding="utf-8")

    def reject_replace(*_args, **_kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(WRITER.os, "replace", reject_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        WRITER.write_source_roster(
            output,
            "camera",
            5004,
            "/run/xgc2/camera.sock",
        )

    assert output.read_text(encoding="utf-8") == "previous roster\n"
    assert temporary_files(tmp_path, output.name) == []


def test_lab_source_mode_cannot_reuse_a_stale_media_edge_binary():
    script = LAB_SCRIPT.read_text(encoding="utf-8")

    assert "CONTROL_SOCKET is run-owned and cannot be overridden" in script
    assert 'RUN_DIR="$(mktemp -d "${WORK}/run.XXXXXX")"' in script
    assert 'CONTROL_SOCKET="${RUN_DIR}/adapter-control.sock"' in script
    assert 'rm -rf -- "${RUN_DIR}"' in script
    assert 'elif [[ ! -x "${WORK}/xgc-media-edge" ]]' not in script
    assert 'go build -o "${EDGE_BINARY_TEMP}"' in script
    assert (
        'mv -f -- "${EDGE_BINARY_TEMP}" "${WORK}/xgc-media-edge"'
        in script
    )
    assert 'if [[ "${healthy}" != "1" ]]' in script


def test_package_matrix_integrates_an_exact_installed_media_edge():
    script = DOCKER_BUILD_SCRIPT.read_text(encoding="utf-8")
    integration_lock = json.loads(INTEGRATION_LOCK.read_text(encoding="utf-8"))

    assert "MEDIA_EDGE_REF" not in script
    assert "dc461a8d2b9a1718fdd7616ce93a52d8dbc326ba" not in script
    assert integration_lock == {
        "schema": "xgc2.integration-lock/v1",
        "mediaEdge": {
            "repository": "https://github.com/lxk36/xgc2-media-edge.git",
            "sourceSha": "bf64868b8ff20bdacf4647536fa86bf15fc0bfa8",
            "version": "0.6.0-5",
        },
        "rosImages": {
            "humble-jammy": "ghcr.io/xgc-team/xgc2-images/xgc2-build-jammy-ros-humble:1.0.0",
            "jazzy-noble": "ghcr.io/xgc-team/xgc2-images/xgc2-build-noble-ros-jazzy:1.0.0",
            "noetic-focal": "ghcr.io/xgc-team/xgc2-images/xgc2-build-focal-ros-noetic:1.0.0",
        },
    }
    assert 'INTEGRATION_LOCK="${REPO_ROOT}/.xgc2/integration-lock.json"' in script
    assert '--field sourceSha' in script
    assert '--field version' in script
    assert 'fetch --depth 1 origin "${MEDIA_EDGE_SHA}"' in script
    assert 'test "$(git -C /workspace/work/media-edge rev-parse HEAD)" = "${MEDIA_EDGE_SHA}"' in script
    assert './.xgc2/scripts/build_deb.sh' in script
    assert 'apt-get install -y /workspace/work/media-edge-debs/xgc2-media-edge_*.deb' in script
    assert 'if [[ "${DEPENDENCY_MODE}" == "staging-apt" ]]' in script
    assert "/workspace/repo/.xgc2/scripts/configure_xgc2_apt.sh" in script
    assert 'apt-get --print-uris download "xgc2-media-edge=${media_edge_candidate}"' in script
    assert '"xgc2-media-edge=${media_edge_candidate}"' in script
    assert '"schema": "xgc2.dependency-evidence.v1"' in script
    assert 'test -x /usr/lib/xgc2-media-edge/mediamtx' in script
    assert 'export MEDIA_EDGE_BINARY=/usr/bin/xgc-media-edge' in script
    assert "https://go.dev/dl/" not in script
    assert "build-essential" not in script
    assert 'env -i' in script


def test_dependency_set_digest_matches_the_central_plan_contract() -> None:
    lock = json.loads(INTEGRATION_LOCK.read_text(encoding="utf-8"))
    expected = hashlib.sha256(
        json.dumps(
            [
                {
                    "id": "xgc2-media-edge",
                    "action": "verify",
                    "source_sha": lock["mediaEdge"]["sourceSha"],
                    "version": lock["mediaEdge"]["version"],
                    "policy": "verify",
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    result = subprocess.run(
        [
            sys.executable,
            str(READ_INTEGRATION_LOCK),
            "--lock",
            str(INTEGRATION_LOCK),
            "--field",
            "dependencySetDigest",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == expected


def test_integration_resolves_only_the_installed_adapter():
    script = INTEGRATION_SCRIPT.read_text(encoding="utf-8")

    assert "EXPECTED_ADAPTER_PREFIX" in script
    assert "WORKSPACE_INSTALL" not in script
    assert 'REPO_ROOT}/install/setup.bash' not in script
    assert "ros2 pkg prefix ros_image_rtp_adapter" in script
    assert "rospack find ros_image_rtp_adapter" in script
    assert "CONTROL_SOCKET is run-owned and cannot be overridden" in script
    assert 'CONTROL_SOCKET="${WORK}/adapter-control.sock"' in script
    assert '"TIMEOUT_SEC:${TIMEOUT_SEC}:1:600"' in script
