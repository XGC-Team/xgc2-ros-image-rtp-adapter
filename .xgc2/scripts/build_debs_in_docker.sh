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
      python3 python3-pip python3-pytest python3-numpy python3-pil \
      ffmpeg curl ca-certificates \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
    if [[ "${ROS_DISTRO}" == "noetic" ]]; then
      apt-get install -y --no-install-recommends \
        ros-noetic-catkin ros-noetic-rospy ros-noetic-sensor-msgs \
        ros-noetic-roslaunch ros-noetic-rosbash ros-noetic-rospack
    else
      apt-get install -y --no-install-recommends \
        python3-colcon-common-extensions \
        "ros-${ROS_DISTRO}-rclpy" "ros-${ROS_DISTRO}-sensor-msgs" \
        "ros-${ROS_DISTRO}-std-msgs" "ros-${ROS_DISTRO}-launch" \
        "ros-${ROS_DISTRO}-launch-ros" "ros-${ROS_DISTRO}-ros2pkg" \
        "ros-${ROS_DISTRO}-ament-cmake"
    fi

    # media-edge declares its exact toolchain floor in go.mod.
    arch="$(dpkg --print-architecture)"
    case "${arch}" in
      amd64) go_arch=amd64 ;;
      arm64) go_arch=arm64 ;;
      *) go_arch="${arch}" ;;
    esac
    curl -fsSL "https://go.dev/dl/go1.26.2.linux-${go_arch}.tar.gz" -o /tmp/go.tgz
    rm -rf /usr/local/go
    tar -C /usr/local -xzf /tmp/go.tgz
    export PATH="/usr/local/go/bin:${PATH:-/usr/bin}"
    go version

    rm -rf /workspace/work/build /workspace/work/install /workspace/work/log \
      /workspace/work/src /workspace/work/source /workspace/work/catkin \
      /workspace/work/install-root
    mkdir -p /workspace/work/source
    rsync -a --delete --exclude .work --exclude debs --exclude .git \
      /workspace/repo/ /workspace/work/source/

    set +u
    source /opt/ros/${ROS_DISTRO}/setup.bash
    set -u
    if [[ "${ROS_DISTRO}" == "noetic" ]]; then
      mkdir -p /workspace/work/catkin/src
      ln -s /workspace/work/source/ros1 \
        /workspace/work/catkin/src/ros_image_rtp_adapter
      cd /workspace/work/catkin
      catkin_make \
        -DROS_IMAGE_RTP_COMMON_DIR=/workspace/work/source/ros_image_rtp_adapter
      set +u
      source devel/setup.bash
      set -u
      export WORKSPACE_INSTALL=/workspace/work/catkin/devel
    else
      mkdir -p /workspace/work/src
      ln -s /workspace/work/source /workspace/work/src/ros_image_rtp_adapter
      cd /workspace/work
      # Release/package staging must contain real files, never source-tree
      # egg-links from a developer symlink install.
      colcon build --packages-select ros_image_rtp_adapter
      set +u
      source install/setup.bash
      set -u
      export WORKSPACE_INSTALL=/workspace/work/install
    fi

    # ROS-neutral unit and real GStreamer RTP tests.
    PYTHONPATH=/workspace/work/source python3 -m pytest \
      /workspace/work/source/test/test_control_socket.py \
      /workspace/work/source/test/test_encoder.py \
      /workspace/work/source/test/test_frames.py \
      /workspace/work/source/test/test_runtime.py -q
    PYTHONPATH=/workspace/work/source \
      python3 /workspace/work/source/scripts/integration_gstreamer_rtp.py

    mkdir -p /workspace/work/install-root
    if [[ "${ROS_DISTRO}" == "noetic" ]]; then
      cd /workspace/work/catkin
      DESTDIR=/workspace/work/install-root catkin_make install \
        -DCMAKE_INSTALL_PREFIX=/opt/ros/noetic \
        -DROS_IMAGE_RTP_COMMON_DIR=/workspace/work/source/ros_image_rtp_adapter
    else
      cd /workspace/work
      # ament_python install layout is copied beneath its ROS prefix.
      mkdir -p /workspace/work/install-root/opt/ros/${ROS_DISTRO}
      if [[ -d install/ros_image_rtp_adapter ]]; then
        rsync -a install/ros_image_rtp_adapter/ \
          /workspace/work/install-root/opt/ros/${ROS_DISTRO}/
      else
        rsync -a install/ /workspace/work/install-root/opt/ros/${ROS_DISTRO}/
      fi
    fi

    /workspace/repo/.xgc2/scripts/package_debs.sh \
      --install-root /workspace/work/install-root \
      --output-dir /workspace/out \
      --ros-distro "${ROS_DISTRO}"

    if [[ "${INSTALL_CHECK}" == "true" ]]; then
      apt-get install -y /workspace/out/ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter_*.deb
      dpkg -L ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter | head
      if [[ "${ROS_DISTRO}" == "noetic" ]]; then
        bash -lc "source /opt/ros/noetic/setup.bash && rospack find ros_image_rtp_adapter"
      else
        bash -lc "source /opt/ros/${ROS_DISTRO}/setup.bash && ros2 pkg prefix ros_image_rtp_adapter"
      fi
    fi

    if [[ "${RUN_INTEGRATION}" == "true" ]]; then
      echo "cloning media-edge for integration (ref=${MEDIA_EDGE_REF})"
      rm -rf /workspace/work/media-edge
      git clone --depth 1 --branch "${MEDIA_EDGE_REF}" "${MEDIA_EDGE_URL}" /workspace/work/media-edge
      export MEDIA_EDGE_DIR=/workspace/work/media-edge
      # repo is mounted read-only; copy scripts into the writable workdir
      cp -a /workspace/repo/scripts /workspace/work/integration-scripts
      chmod +x /workspace/work/integration-scripts/*.sh /workspace/work/integration-scripts/*.py || true
      ROS_DISTRO="${ROS_DISTRO}" /workspace/work/integration-scripts/integration_media_edge.sh
    fi
  '

echo "Debian package output:"
find "${OUTPUT_DIR}" -maxdepth 1 -type f -name "*.deb" -print | sort
