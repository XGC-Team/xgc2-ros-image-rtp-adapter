#!/usr/bin/env bash
# shellcheck disable=SC2016 # The checks intentionally match literal shell source.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"
export PYTHONDONTWRITEBYTECODE=1

test -f package.xml
test -f setup.py
test -f .xgc2/product.yml
test -f ros_image_rtp_adapter/node.py
test -f ros_image_rtp_adapter/ros1_node.py
test -f ros_image_rtp_adapter/runtime.py
test -f ros_image_rtp_adapter/settings.py
test -f ros_image_rtp_adapter/frames.py
test -f launch/image_rtp_adapter.launch.py
test -f config/jetson_nvmm_gstreamer.yaml
test -f ros1/package.xml
test -x ros1/scripts/image_rtp_adapter
test -f ros1/config/jetson_nvmm_gstreamer.yaml
test -f scripts/integration_media_edge.sh
test -f scripts/integration_gstreamer_rtp.py
test -f scripts/write_media_edge_source_roster.py
test -f test/test_media_edge_source_roster.py
test -f test/test_artifact_manifest.py
test -f .xgc2/integration-lock.json
test -f .xgc2/scripts/read_integration_lock.py
test -f .xgc2/scripts/configure_xgc2_apt.sh

# Release quality is mandatory inside every build matrix entry. Do not expose
# switches that let a dispatcher skip source or quality verification.
if grep -RInE 'run_cpp_quality|run_source_tests' .github/workflows; then
  echo "optional release quality gates were reintroduced" >&2
  exit 1
fi
for release_contract_marker in \
  '--prepare-action "${PREPARE_ACTION}"' \
  '--dependency-mode "${dependency_mode}"' \
  '--dependency-set-digest "${DEPENDENCY_SET_DIGEST}"' \
  '--apt-overlay-url "${APT_OVERLAY_URL}"' \
  '--dependency-evidence debs/xgc2-dependency-evidence.json'; do
  grep -Fq -- "${release_contract_marker}" .github/workflows/release.yml
done
if grep -RInE 'uses:[[:space:]]+[^#[:space:]]+@(v[0-9]+|main|master|latest)([[:space:]#]|$)' \
    .github/workflows; then
  echo "GitHub Actions must be pinned by a full commit SHA" >&2
  exit 1
fi
python3 - .github/workflows/ci.yml .github/workflows/release.yml <<'PY'
from pathlib import Path
import re
import sys

for name in sys.argv[1:]:
    source = Path(name).read_text(encoding="utf-8")
    for line_number, line in enumerate(source.splitlines(), start=1):
        match = re.search(r"\buses:\s*([^\s#]+)", line)
        if match and not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", match.group(1)):
            raise SystemExit(f"{name}:{line_number}: action is not pinned by a full SHA")
PY

# Breaking migrations stay deleted. Reintroducing a shadow manifest, board
# alias, or pre-v0.4 encoder API must fail before packaging.
for retired in \
  .xgc2/product-humble.yml \
  .xgc2/product-noetic.yml \
  config/jetson_thor_gstreamer.yaml \
  ros1/config/jetson_thor_gstreamer.yaml; do
  if [[ -e "${retired}" || -L "${retired}" ]]; then
    echo "retired compatibility file exists: ${retired}" >&2
    exit 1
  fi
done
if grep -RInE \
    'SubprocessJpegRtpEncoder|FFmpegJpegRtpEncoder|GStreamerJpegRtpEncoder|create_jpeg_rtp_encoder|write_jpeg' \
    ros_image_rtp_adapter scripts test; then
  echo "retired encoder API was reintroduced" >&2
  exit 1
fi

version="$(awk -F': *' '/^version:[[:space:]]*/ {print $2; exit}' .xgc2/product.yml)"
test -n "${version}"
grep -Fq "<version>${version}</version>" package.xml
grep -Fq "version=\"${version}\"" setup.py
grep -Fq "<version>${version}</version>" ros1/package.xml
grep -Fxq 'kind: mixed' .xgc2/product.yml
grep -A1 -Fxq '  dependency_policy:' .xgc2/product.yml
grep -Fxq '    xgc2-media-edge: verify' .xgc2/product.yml
if grep -q '^ros:' .xgc2/product.yml; then
  echo "mixed ROS 1/ROS 2 product must not project one distro as canonical" >&2
  exit 1
fi
for owned_path in \
  /opt/ros/noetic/lib/pkgconfig/ros_image_rtp_adapter.pc \
  /opt/ros/humble/lib/python3.10/site-packages/ros_image_rtp_adapter \
  /opt/ros/humble/lib/python3.10/site-packages/ros_image_rtp_adapter-${version}-py3.10.egg-info \
  /opt/ros/humble/share/ament_index/resource_index/packages/ros_image_rtp_adapter \
  /opt/ros/humble/share/colcon-core/packages/ros_image_rtp_adapter \
  /opt/ros/jazzy/share/ament_index/resource_index/packages/ros_image_rtp_adapter \
  /opt/ros/jazzy/lib/python3.12/site-packages/ros_image_rtp_adapter-${version}-py3.12.egg-info \
  /opt/ros/jazzy/share/colcon-core/packages/ros_image_rtp_adapter \
  /usr/share/doc/ros-noetic-xgc2-ros-image-rtp-adapter \
  /usr/share/doc/ros-humble-xgc2-ros-image-rtp-adapter \
  /usr/share/doc/ros-jazzy-xgc2-ros-image-rtp-adapter; do
  grep -Fxq "  - ${owned_path}" .xgc2/product.yml
done
for runtime_dependency in \
  libavcodec-extra \
  ros-noetic-rospy ros-noetic-sensor-msgs \
  ros-humble-rclpy ros-humble-sensor-msgs ros-humble-std-msgs \
  ros-humble-launch ros-humble-launch-ros \
  ros-jazzy-rclpy ros-jazzy-sensor-msgs ros-jazzy-std-msgs \
  ros-jazzy-launch ros-jazzy-launch-ros; do
  grep -Fxq "  - ${runtime_dependency}" .xgc2/product.yml
done
grep -Fq 'libavcodec-extra' .xgc2/scripts/package_debs.sh
for common_ros_dependency in \
  python3-numpy python3-pil ffmpeg libavcodec-extra \
  gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly; do
  grep -Fq "<exec_depend>${common_ros_dependency}</exec_depend>" package.xml
  grep -Fq "<exec_depend>${common_ros_dependency}</exec_depend>" ros1/package.xml
done

# Topic must not be hard-coded to a brand path as the only option.
if grep -RIn --include='*.py' '/odin1/image' \
    ros_image_rtp_adapter/node.py ros_image_rtp_adapter/ros1_node.py; then
  echo "ROS wrappers must not hard-code /odin1 topics; use parameters" >&2
  exit 1
fi

# Both ROS wrappers must consume the single shared parameter contract.
grep -q 'PARAMETER_DEFAULTS' ros_image_rtp_adapter/node.py
grep -q 'PARAMETER_DEFAULTS' ros_image_rtp_adapter/ros1_node.py
grep -q '"input_message_type"' ros_image_rtp_adapter/settings.py
grep -q '"raw_encoding"' ros_image_rtp_adapter/settings.py

# Vendor element factories belong in opt-in deployment profiles, never in the
# runtime implementation. This keeps the package usable on non-NVIDIA devices.
if grep -RInE --include='*.py' 'nvjpegdec|nvvidconv|nvv4l2h264enc|Jetson|Thor' \
    ros_image_rtp_adapter; then
  echo "runtime Python must not hard-code a vendor or board profile" >&2
  exit 1
fi

python3 -m py_compile ros_image_rtp_adapter/control_socket.py
python3 -m py_compile ros_image_rtp_adapter/encoder.py
python3 -m py_compile ros_image_rtp_adapter/frames.py
python3 -m py_compile ros_image_rtp_adapter/node.py
python3 -m py_compile ros_image_rtp_adapter/ros1_node.py
python3 -m py_compile ros_image_rtp_adapter/runtime.py
python3 -m py_compile ros_image_rtp_adapter/settings.py
python3 -m py_compile ros_image_rtp_adapter/publish_test_jpeg.py
python3 -m py_compile launch/image_rtp_adapter.launch.py
python3 -m py_compile scripts/integration_gstreamer_rtp.py
python3 -m py_compile scripts/write_media_edge_source_roster.py
python3 -m py_compile .xgc2/scripts/read_integration_lock.py
python3 -m py_compile .xgc2/scripts/xgc2_artifact_manifest.py
python3 .xgc2/scripts/read_integration_lock.py --lock .xgc2/integration-lock.json
test "$(python3 .xgc2/scripts/read_integration_lock.py \
  --lock .xgc2/integration-lock.json --field goVersion)" = "1.26.2"
for architecture in amd64 arm64; do
  test "$(python3 .xgc2/scripts/read_integration_lock.py \
    --lock .xgc2/integration-lock.json --field goSha256 \
    --architecture "${architecture}" | wc -c)" = "65"
done
python3 -m py_compile ros1/scripts/image_rtp_adapter ros1/scripts/publish_test_jpeg
bash -n scripts/integration_media_edge.sh scripts/lab_video_preview.sh
bash -n \
  .xgc2/scripts/build_debs_in_docker.sh \
  .xgc2/scripts/configure_xgc2_apt.sh \
  .xgc2/scripts/package_debs.sh
command -v shellcheck >/dev/null
shellcheck \
  .xgc2/scripts/build_debs_in_docker.sh \
  .xgc2/scripts/check_package_compliance.sh \
  .xgc2/scripts/configure_xgc2_apt.sh \
  .xgc2/scripts/package_debs.sh \
  scripts/integration_media_edge.sh \
  scripts/lab_video_preview.sh

for caller in scripts/integration_media_edge.sh scripts/lab_video_preview.sh; do
  grep -Fq 'write_media_edge_source_roster.py' "${caller}"
  grep -Fq -- '-sources-config "${SOURCES_CONFIG}"' "${caller}"
  if grep -Eq -- '-rtp-listen-address|-source-control-socket' "${caller}"; then
    echo "${caller} still invokes a retired Media Edge source flag" >&2
    exit 1
  fi
done

# The persistent lab workspace must never make an executable cache hit the
# source of truth. Every source-mode run rebuilds and atomically installs Edge.
if grep -Fq 'elif [[ ! -x "${WORK}/xgc-media-edge" ]]' scripts/lab_video_preview.sh; then
  echo "lab preview must not reuse a stale Media Edge binary" >&2
  exit 1
fi
grep -Fq 'go build -o "${EDGE_BINARY_TEMP}"' scripts/lab_video_preview.sh
grep -Fq 'mv -f -- "${EDGE_BINARY_TEMP}" "${WORK}/xgc-media-edge"' scripts/lab_video_preview.sh
grep -Fq 'if [[ "${healthy}" != "1" ]]' scripts/lab_video_preview.sh
grep -Fq 'CONTROL_SOCKET is run-owned and cannot be overridden' scripts/lab_video_preview.sh
grep -Fq 'CONTROL_SOCKET="${RUN_DIR}/adapter-control.sock"' scripts/lab_video_preview.sh

# Integration must resolve the installed Deb under /opt/ros, never a build or
# devel overlay left in the packaging workspace.
grep -Fq 'EXPECTED_ADAPTER_PREFIX' scripts/integration_media_edge.sh
if grep -Eq 'WORKSPACE_INSTALL|REPO_ROOT.*/install/setup\.bash' scripts/integration_media_edge.sh; then
  echo "integration must not source a workspace adapter overlay" >&2
  exit 1
fi
for player_contract_marker in \
  'id="app" data-source-id=' \
  'href="/assets/player.css"' \
  'type="module" src="/assets/player.js"' \
  'RTCPeerConnection' \
  '.media-player-page'; do
  grep -Fq "${player_contract_marker}" scripts/integration_media_edge.sh
done
if grep -Fq 'webrtc\|video\|session' scripts/integration_media_edge.sh; then
  echo "integration must validate the player contract, not visible copy" >&2
  exit 1
fi

# Push CI uses an immutable source lock; release-scoped builds install the
# signed APT candidate. Both paths produce exact dependency evidence and run the
# installed package, including its MediaMTX child.
grep -Fq 'INTEGRATION_LOCK="${REPO_ROOT}/.xgc2/integration-lock.json"' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq 'apt-get install -y /workspace/work/media-edge-debs/xgc2-media-edge_*.deb' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq '/workspace/repo/.xgc2/scripts/configure_xgc2_apt.sh' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq 'apt-get --print-uris download "xgc2-media-edge=${media_edge_candidate}"' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq '"xgc2-media-edge=${media_edge_candidate}"' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq '"schema": "xgc2.dependency-evidence.v1"' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq 'test -x /usr/lib/xgc2-media-edge/mediamtx' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq 'sha256sum -c -' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq 'env -i' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq 'SOURCE_DATE_EPOCH' .xgc2/scripts/package_debs.sh
grep -Fq 'Recommends: xgc2-media-edge' .xgc2/scripts/package_debs.sh
grep -Fq 'generated Config.cmake has an unexpected source prefix' \
  .xgc2/scripts/package_debs.sh
grep -Fq 'generated Config.cmake has an unexpected devel prefix' \
  .xgc2/scripts/package_debs.sh
grep -Fq '/workspace/work/repro/install-root' .xgc2/scripts/build_debs_in_docker.sh
grep -Fq -- '--mount type=volume,destination=/workspace/work,volume-nocopy' \
  .xgc2/scripts/build_debs_in_docker.sh
for contract_marker in \
  'xgc2.build-artifact.v2' \
  'prepareAction' \
  'dependencySetDigest' \
  'dependencyMode' \
  'dependencies'; do
  grep -Fq "${contract_marker}" .xgc2/scripts/xgc2_artifact_manifest.py
done
if grep -Fq 'xgc2.build-artifact.v1' .xgc2/scripts/xgc2_artifact_manifest.py; then
  echo "build artifact v1 fallback was reintroduced" >&2
  exit 1
fi
if grep -Eq -- '--work-dir|WORK_DIR' .xgc2/scripts/build_debs_in_docker.sh \
    .github/workflows/ci.yml .github/workflows/release.yml; then
  echo "host-mounted build scratch was reintroduced" >&2
  exit 1
fi
if grep -Fq '|| true' .xgc2/scripts/package_debs.sh; then
  echo "package builder must not swallow owned-layout copy failures" >&2
  exit 1
fi
python3 - .xgc2/scripts/package_debs.sh <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
marker = 'if [[ "${actual_files}" != "${expected_files}" ]]; then'
start = source.find(marker)
end = source.find("\nfi", start)
if start < 0 or end < 0 or "exit 1" not in source[start:end]:
    raise SystemExit("exact ownership mismatch is not fail-closed")
PY

# CLI resolution is independent of option order and rejects invalid pairs or
# floating image overrides without starting Docker.
dependency_set_digest="$(python3 .xgc2/scripts/read_integration_lock.py \
  --lock .xgc2/integration-lock.json --field dependencySetDigest)"
contract_args=(
  --prepare-action ci
  --dependency-mode locked-source
  --dependency-set-digest "${dependency_set_digest}"
)
config_a="$(env DOCKER_IMAGE= ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
  .xgc2/scripts/build_debs_in_docker.sh \
  "${contract_args[@]}" \
  --ubuntu jammy --ros-distro humble --print-config)"
config_b="$(env DOCKER_IMAGE= ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
  .xgc2/scripts/build_debs_in_docker.sh \
  --ros-distro humble --ubuntu jammy \
  "${contract_args[@]}" --print-config)"
test "${config_a}" = "${config_b}"
grep -Fq 'ros_distro=humble' <<<"${config_a}"
grep -Fq 'ubuntu=jammy' <<<"${config_a}"
grep -Eq '^image=docker\.io/library/ros@sha256:[0-9a-f]{64}$' <<<"${config_a}"
if env DOCKER_IMAGE= ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
    .xgc2/scripts/build_debs_in_docker.sh \
    "${contract_args[@]}" \
    --ros-distro noetic --ubuntu noble --print-config >/dev/null 2>&1; then
  echo "build CLI accepted an invalid ROS/Ubuntu pair" >&2
  exit 1
fi
if env DOCKER_IMAGE= ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
    .xgc2/scripts/build_debs_in_docker.sh \
    "${contract_args[@]}" \
    --image ros:jazzy-ros-base-noble --print-config >/dev/null 2>&1; then
  echo "build CLI accepted a floating Docker image" >&2
  exit 1
fi
if env DOCKER_IMAGE= ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
    .xgc2/scripts/build_debs_in_docker.sh \
    "${contract_args[@]}" --output-dir / \
    --print-config >/dev/null 2>&1; then
  echo "build CLI accepted / as the output directory" >&2
  exit 1
fi
if env DOCKER_IMAGE= ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
    .xgc2/scripts/build_debs_in_docker.sh \
    "${contract_args[@]}" --work-dir /tmp \
    --print-config >/dev/null 2>&1; then
  echo "build CLI accepted the retired host scratch option" >&2
  exit 1
fi
if env DOCKER_IMAGE= ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
    .xgc2/scripts/build_debs_in_docker.sh \
    "${contract_args[@]}" --output-dir "${ROOT}/scripts" \
    --print-config >/dev/null 2>&1; then
  echo "build CLI accepted an output directory outside the repository debs tree" >&2
  exit 1
fi
if env ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
    .xgc2/scripts/build_debs_in_docker.sh \
    --prepare-action compatibility-verify \
    --dependency-mode locked-source \
    --dependency-set-digest "${dependency_set_digest}" \
    --print-config >/dev/null 2>&1; then
  echo "compatibility-verify accepted locked-source fallback" >&2
  exit 1
fi
if env ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
    .xgc2/scripts/build_debs_in_docker.sh \
    --prepare-action compatibility-verify \
    --dependency-mode staging-apt \
    --dependency-set-digest "${dependency_set_digest}" \
    --print-config >/dev/null 2>&1; then
  echo "staging-apt accepted a missing overlay URL" >&2
  exit 1
fi
if env ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
    .xgc2/scripts/build_debs_in_docker.sh \
    --prepare-action compatibility-verify \
    --dependency-mode staging-apt \
    --dependency-set-digest "${dependency_set_digest}" \
    --apt-overlay-url http://apt.example/staging/release \
    --print-config >/dev/null 2>&1; then
  echo "staging-apt accepted an untrusted HTTP overlay URL" >&2
  exit 1
fi
staging_config="$(env ROS_DISTRO=jazzy UBUNTU_CODENAME=noble \
  .xgc2/scripts/build_debs_in_docker.sh \
  --prepare-action compatibility-verify \
  --dependency-mode staging-apt \
  --dependency-set-digest "${dependency_set_digest}" \
  --apt-overlay-url https://apt.example/staging/release \
  --print-config)"
grep -Fq 'dependency_mode=staging-apt' <<<"${staging_config}"
grep -Fq 'apt_overlay_url=https://apt.example/staging/release' <<<"${staging_config}"

# A staged install tree is an ownership boundary. The packager must reject a
# link before any owned-directory copy can follow it out of that boundary.
package_probe="$(mktemp -d)"
trap 'rm -rf -- "${package_probe}"' EXIT
mkdir -p "${package_probe}/install/opt/ros/jazzy" "${package_probe}/output"
ln -s /etc/passwd "${package_probe}/install/opt/ros/jazzy/owned-link"
if SOURCE_DATE_EPOCH=1 .xgc2/scripts/package_debs.sh \
    --install-root "${package_probe}/install" \
    --output-dir "${package_probe}/output" \
    --ros-distro jazzy >/dev/null 2>&1; then
  echo "package builder followed a staged symbolic link" >&2
  exit 1
fi
rm -rf -- "${package_probe}"
trap - EXIT

echo "compliance OK"
