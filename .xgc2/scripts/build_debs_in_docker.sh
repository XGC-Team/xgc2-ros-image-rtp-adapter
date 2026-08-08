#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ROS_DISTRO="${ROS_DISTRO:-jazzy}"
UBUNTU_CODENAME="${UBUNTU_CODENAME:-noble}"
DOCKER_IMAGE="${DOCKER_IMAGE:-ros:${ROS_DISTRO}-ros-base-${UBUNTU_CODENAME}}"
WORK_DIR="${WORK_DIR:-${REPO_ROOT}/.work/docker-${ROS_DISTRO}}"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/debs}"
INSTALL_CHECK="${INSTALL_CHECK:-true}"
EXPECTED_ARCH="${EXPECTED_ARCH:-}"
RUN_INTEGRATION="${RUN_INTEGRATION:-true}"
MEDIA_EDGE_URL="${MEDIA_EDGE_URL:-https://github.com/lxk36/xgc2-media-edge.git}"
MEDIA_EDGE_REF="${MEDIA_EDGE_REF:-main}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) DOCKER_IMAGE="$2"; shift 2 ;;
    --ros-distro) ROS_DISTRO="$2"; shift 2 ;;
    --ubuntu) UBUNTU_CODENAME="$2"; DOCKER_IMAGE="ros:${ROS_DISTRO}-ros-base-${UBUNTU_CODENAME}"; shift 2 ;;
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --skip-install-check) INSTALL_CHECK=false; shift ;;
    --skip-integration) RUN_INTEGRATION=false; shift ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "${WORK_DIR}" "${OUTPUT_DIR}"

docker pull "${DOCKER_IMAGE}"
docker run --rm \
  -e DEBIAN_FRONTEND=noninteractive \
  -e EXPECTED_ARCH="${EXPECTED_ARCH}" \
  -e INSTALL_CHECK="${INSTALL_CHECK}" \
  -e RUN_INTEGRATION="${RUN_INTEGRATION}" \
  -e ROS_DISTRO="${ROS_DISTRO}" \
  -e MEDIA_EDGE_URL="${MEDIA_EDGE_URL}" \
  -e MEDIA_EDGE_REF="${MEDIA_EDGE_REF}" \
  -v "${REPO_ROOT}:/workspace/repo:ro" \
  -v "${WORK_DIR}:/workspace/work" \
  -v "${OUTPUT_DIR}:/workspace/out" \
  "${DOCKER_IMAGE}" bash -lc '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    actual_arch="$(dpkg --print-architecture)"
    if [[ -n "${EXPECTED_ARCH}" && "${actual_arch}" != "${EXPECTED_ARCH}" ]]; then
      echo "container architecture ${actual_arch} != expected ${EXPECTED_ARCH}" >&2
      exit 1
    fi

    apt-get update
    apt-get install -y --no-install-recommends \
      build-essential cmake dpkg-dev fakeroot file git rsync \
      python3 python3-pip python3-colcon-common-extensions python3-pytest python3-numpy python3-pil \
      ffmpeg curl ca-certificates \
      "ros-${ROS_DISTRO}-rclpy" "ros-${ROS_DISTRO}-sensor-msgs" "ros-${ROS_DISTRO}-std-msgs" \
      "ros-${ROS_DISTRO}-launch" "ros-${ROS_DISTRO}-launch-ros" "ros-${ROS_DISTRO}-ros2pkg" \
      "ros-${ROS_DISTRO}-ament-cmake" || true

    # media-edge requires a recent Go toolchain (module go 1.22+).
    arch="$(dpkg --print-architecture)"
    case "${arch}" in
      amd64) go_arch=amd64 ;;
      arm64) go_arch=arm64 ;;
      *) go_arch="${arch}" ;;
    esac
    curl -fsSL "https://go.dev/dl/go1.22.10.linux-${go_arch}.tar.gz" -o /tmp/go.tgz
    rm -rf /usr/local/go
    tar -C /usr/local -xzf /tmp/go.tgz
    export PATH="/usr/local/go/bin:${PATH:-/usr/bin}"
    go version

    rm -rf /workspace/work/build /workspace/work/install /workspace/work/log /workspace/work/src /workspace/work/install-root
    mkdir -p /workspace/work/src
    rsync -a --delete --exclude .work --exclude debs --exclude .git /workspace/repo/ /workspace/work/src/ros_image_rtp_adapter/

    cd /workspace/work
    set +u
    source /opt/ros/${ROS_DISTRO}/setup.bash
    set -u
    colcon build --packages-select ros_image_rtp_adapter --symlink-install
    set +u
    source install/setup.bash
    set -u

    # unit tests (control socket, no ROS daemon required for pure unit)
    python3 -m pytest /workspace/work/src/ros_image_rtp_adapter/test/test_control_socket.py -q

    # Stage install for deb
    mkdir -p /workspace/work/install-root
    # colcon install layout already under install/
    # Reinstall into DESTDIR-like tree via pip/ament path copy
    colcon build --packages-select ros_image_rtp_adapter \
      --cmake-args -DCMAKE_INSTALL_PREFIX=/opt/ros/${ROS_DISTRO} 2>/dev/null || true
    # ament_python: copy install tree
    mkdir -p /workspace/work/install-root/opt/ros/${ROS_DISTRO}
    rsync -a install/ /workspace/work/install-root/opt/ros/${ROS_DISTRO}/merge 2>/dev/null || true
    # Prefer isolated package install layout
    if [[ -d install/ros_image_rtp_adapter ]]; then
      rsync -a install/ros_image_rtp_adapter/ /workspace/work/install-root/opt/ros/${ROS_DISTRO}/
    else
      # merged install
      rsync -a install/ /workspace/work/install-root/opt/ros/${ROS_DISTRO}/
    fi

    /workspace/repo/.xgc2/scripts/package_debs.sh \
      --install-root /workspace/work/install-root \
      --output-dir /workspace/out \
      --ros-distro "${ROS_DISTRO}"

    if [[ "${INSTALL_CHECK}" == "true" ]]; then
      apt-get install -y /workspace/out/ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter_*.deb
      dpkg -L ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter | head
      bash -lc "source /opt/ros/${ROS_DISTRO}/setup.bash && ros2 pkg prefix ros_image_rtp_adapter"
    fi

    if [[ "${RUN_INTEGRATION}" == "true" ]]; then
      echo "cloning media-edge for integration (ref=${MEDIA_EDGE_REF})"
      rm -rf /workspace/work/media-edge
      git clone --depth 1 --branch "${MEDIA_EDGE_REF}" "${MEDIA_EDGE_URL}" /workspace/work/media-edge
      export MEDIA_EDGE_DIR=/workspace/work/media-edge
      export WORKSPACE_INSTALL=/workspace/work/install
      # repo is mounted read-only; copy scripts into the writable workdir
      cp -a /workspace/repo/scripts /workspace/work/integration-scripts
      chmod +x /workspace/work/integration-scripts/*.sh /workspace/work/integration-scripts/*.py || true
      ROS_DISTRO="${ROS_DISTRO}" /workspace/work/integration-scripts/integration_media_edge.sh
    fi
  '

echo "Debian package output:"
find "${OUTPUT_DIR}" -maxdepth 1 -type f -name "*.deb" -print | sort
