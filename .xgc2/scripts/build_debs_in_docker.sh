#!/usr/bin/env bash
# shellcheck disable=SC1004 # Inner bash receives and parses these continuations.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ROS_DISTRO="${ROS_DISTRO:-jazzy}"
UBUNTU_CODENAME="${UBUNTU_CODENAME:-noble}"
DOCKER_IMAGE="${DOCKER_IMAGE:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
INSTALL_CHECK="${INSTALL_CHECK:-true}"
EXPECTED_ARCH="${EXPECTED_ARCH:-}"
RUN_INTEGRATION="${RUN_INTEGRATION:-true}"
PREPARE_ACTION=""
DEPENDENCY_MODE=""
APT_OVERLAY_URL=""
DEPENDENCY_SET_DIGEST=""
PRINT_CONFIG=false
INTEGRATION_LOCK="${REPO_ROOT}/.xgc2/integration-lock.json"
LOCK_READER="${REPO_ROOT}/.xgc2/scripts/read_integration_lock.py"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) DOCKER_IMAGE="${2:?missing --image value}"; shift 2 ;;
    --ros-distro) ROS_DISTRO="${2:?missing --ros-distro value}"; shift 2 ;;
    --ubuntu) UBUNTU_CODENAME="${2:?missing --ubuntu value}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:?missing --output-dir value}"; shift 2 ;;
    --prepare-action) PREPARE_ACTION="${2:?missing --prepare-action value}"; shift 2 ;;
    --dependency-mode) DEPENDENCY_MODE="${2:?missing --dependency-mode value}"; shift 2 ;;
    --apt-overlay-url) APT_OVERLAY_URL="${2:?missing --apt-overlay-url value}"; shift 2 ;;
    --dependency-set-digest) DEPENDENCY_SET_DIGEST="${2:?missing --dependency-set-digest value}"; shift 2 ;;
    --skip-install-check) INSTALL_CHECK=false; shift ;;
    --skip-integration) RUN_INTEGRATION=false; shift ;;
    --print-config) PRINT_CONFIG=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

case "${ROS_DISTRO}:${UBUNTU_CODENAME}" in
  noetic:focal|humble:jammy|jazzy:noble) ;;
  *)
    echo "unsupported ROS/Ubuntu pair: ${ROS_DISTRO}/${UBUNTU_CODENAME}" >&2
    exit 1
    ;;
esac
case "${INSTALL_CHECK}" in true|false) ;; *) echo "INSTALL_CHECK must be true or false" >&2; exit 1 ;; esac
case "${RUN_INTEGRATION}" in true|false) ;; *) echo "RUN_INTEGRATION must be true or false" >&2; exit 1 ;; esac
case "${EXPECTED_ARCH}" in ""|amd64|arm64) ;; *) echo "EXPECTED_ARCH must be amd64 or arm64" >&2; exit 1 ;; esac
case "${PREPARE_ACTION}" in
  ci|release|compatibility-verify) ;;
  *) echo "--prepare-action must be ci, release, or compatibility-verify" >&2; exit 1 ;;
esac
case "${DEPENDENCY_MODE}" in
  locked-source|staging-apt) ;;
  *) echo "--dependency-mode must be locked-source or staging-apt" >&2; exit 1 ;;
esac
if [[ ! "${DEPENDENCY_SET_DIGEST}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "--dependency-set-digest must be 64 lowercase hexadecimal characters" >&2
  exit 1
fi
https_url_pattern='^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%+:/=-]*)?$'
if [[ -n "${APT_OVERLAY_URL}" ]]; then
  if [[ "${APT_OVERLAY_URL}" =~ [[:space:][:cntrl:]] ||
        ! "${APT_OVERLAY_URL}" =~ ${https_url_pattern} ]]; then
    echo "--apt-overlay-url must be an HTTPS URL without credentials, query, fragment, whitespace, or control characters" >&2
    exit 1
  fi
  APT_OVERLAY_URL="${APT_OVERLAY_URL%/}"
fi
if [[ "${PREPARE_ACTION}" == "ci" && "${DEPENDENCY_MODE}" != "locked-source" ]]; then
  echo "CI builds must use locked-source dependency mode" >&2
  exit 1
fi
if [[ "${PREPARE_ACTION}" == "compatibility-verify" &&
      "${DEPENDENCY_MODE}" != "staging-apt" ]]; then
  echo "compatibility-verify must use staging-apt dependency mode" >&2
  exit 1
fi
if [[ "${DEPENDENCY_MODE}" == "staging-apt" && -z "${APT_OVERLAY_URL}" ]]; then
  echo "staging-apt dependency mode requires --apt-overlay-url" >&2
  exit 1
fi
if [[ "${DEPENDENCY_MODE}" == "locked-source" && -n "${APT_OVERLAY_URL}" ]]; then
  echo "locked-source dependency mode forbids --apt-overlay-url" >&2
  exit 1
fi

python3 "${LOCK_READER}" --lock "${INTEGRATION_LOCK}"
locked_docker_image="$(python3 "${LOCK_READER}" --lock "${INTEGRATION_LOCK}" \
  --field rosImage --ros-distro "${ROS_DISTRO}" --ubuntu "${UBUNTU_CODENAME}")"
if [[ -z "${DOCKER_IMAGE}" ]]; then
  DOCKER_IMAGE="${locked_docker_image}"
elif [[ "${DOCKER_IMAGE}" != "${locked_docker_image}" ]]; then
  echo "explicit Docker image must match the approved XGC2 build image for ${ROS_DISTRO}/${UBUNTU_CODENAME}" >&2
  exit 1
fi
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/debs}"
MEDIA_EDGE_URL="$(python3 "${LOCK_READER}" --lock "${INTEGRATION_LOCK}" --field repository)"
MEDIA_EDGE_SHA="$(python3 "${LOCK_READER}" --lock "${INTEGRATION_LOCK}" --field sourceSha)"
MEDIA_EDGE_VERSION="$(python3 "${LOCK_READER}" --lock "${INTEGRATION_LOCK}" --field version)"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "${REPO_ROOT}" log -1 --format=%ct)}"
if [[ ! "${SOURCE_DATE_EPOCH}" =~ ^[0-9]+$ ]]; then
  echo "SOURCE_DATE_EPOCH must be a non-negative integer" >&2
  exit 1
fi
case "${OUTPUT_DIR}" in
  /*) ;;
  *) echo "output directory must be absolute" >&2; exit 1 ;;
esac
OUTPUT_DIR="$(realpath -m -- "${OUTPUT_DIR}")"
if [[ "${OUTPUT_DIR}" == "/" ]]; then
  echo "resolved output directory must not be /" >&2
  exit 1
fi
REPO_ROOT="$(realpath -e -- "${REPO_ROOT}")"
case "${REPO_ROOT}/" in "${OUTPUT_DIR}/"*) echo "output directory must not contain the repository" >&2; exit 1 ;; esac
case "${OUTPUT_DIR}" in
  "${REPO_ROOT}"/debs|"${REPO_ROOT}"/debs/*) ;;
  "${REPO_ROOT}"|"${REPO_ROOT}"/*)
    echo "output directory inside the repository must be beneath ${REPO_ROOT}/debs" >&2
    exit 1
    ;;
esac

if [[ "${PRINT_CONFIG}" == "true" ]]; then
  printf 'ros_distro=%s\nubuntu=%s\nimage=%s\nwork_storage=anonymous-volume\noutput_dir=%s\nprepare_action=%s\ndependency_mode=%s\napt_overlay_url=%s\ndependency_set_digest=%s\n' \
    "${ROS_DISTRO}" "${UBUNTU_CODENAME}" "${DOCKER_IMAGE}" "${OUTPUT_DIR}" \
    "${PREPARE_ACTION}" "${DEPENDENCY_MODE}" "${APT_OVERLAY_URL}" \
    "${DEPENDENCY_SET_DIGEST}"
  exit 0
fi

mkdir -p "${OUTPUT_DIR}"

docker pull "${DOCKER_IMAGE}"
docker run --rm \
  -e DEBIAN_FRONTEND=noninteractive \
  -e EXPECTED_ARCH="${EXPECTED_ARCH}" \
  -e INSTALL_CHECK="${INSTALL_CHECK}" \
  -e PREPARE_ACTION="${PREPARE_ACTION}" \
  -e DEPENDENCY_MODE="${DEPENDENCY_MODE}" \
  -e APT_OVERLAY_URL="${APT_OVERLAY_URL}" \
  -e DEPENDENCY_SET_DIGEST="${DEPENDENCY_SET_DIGEST}" \
  -e RUN_INTEGRATION="${RUN_INTEGRATION}" \
  -e ROS_DISTRO="${ROS_DISTRO}" \
  -e UBUNTU_CODENAME="${UBUNTU_CODENAME}" \
  -e MEDIA_EDGE_URL="${MEDIA_EDGE_URL}" \
  -e MEDIA_EDGE_SHA="${MEDIA_EDGE_SHA}" \
  -e MEDIA_EDGE_VERSION="${MEDIA_EDGE_VERSION}" \
  -e SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
  -v "${REPO_ROOT}:/workspace/repo:ro" \
  --mount type=volume,destination=/workspace/work,volume-nocopy \
  -v "${OUTPUT_DIR}:/workspace/out" \
  "${DOCKER_IMAGE}" bash -lc '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    actual_arch="$(dpkg --print-architecture)"
    if [[ -n "${EXPECTED_ARCH}" && "${actual_arch}" != "${EXPECTED_ARCH}" ]]; then
      echo "container architecture ${actual_arch} != expected ${EXPECTED_ARCH}" >&2
      exit 1
    fi
    source /etc/os-release
    if [[ "${VERSION_CODENAME:-}" != "${UBUNTU_CODENAME}" ]]; then
      echo "container Ubuntu ${VERSION_CODENAME:-unknown} != expected ${UBUNTU_CODENAME}" >&2
      exit 1
    fi
    test -f "/opt/ros/${ROS_DISTRO}/setup.bash"

    if [[ "${DEPENDENCY_MODE}" == "staging-apt" ]]; then
      /bin/bash /workspace/repo/.xgc2/scripts/configure_xgc2_apt.sh \
        "${APT_OVERLAY_URL}" "${UBUNTU_CODENAME}"
    fi

    # Compile dependencies and toolchains are owned by the approved XGC2 image.
    for command in cmake dpkg-deb fakeroot ffmpeg git go gst-launch-1.0 \
      python3 rsync; do
      command -v "${command}" >/dev/null
    done
    python3 -c "import numpy, PIL, pytest"
    go version

    rm -rf /workspace/work/build /workspace/work/install /workspace/work/log \
      /workspace/work/src /workspace/work/source /workspace/work/catkin \
      /workspace/work/install-root /workspace/work/repro \
      /workspace/work/integration-scripts /workspace/work/media-edge \
      /workspace/work/media-edge-debs
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
    else
      mkdir -p /workspace/work/src
      ln -s /workspace/work/source /workspace/work/src/ros_image_rtp_adapter
      cd /workspace/work
      # Release/package staging must contain real files, never source-tree
      # egg-links from a developer symlink install.
      colcon build --packages-select ros_image_rtp_adapter
    fi

    # ROS-neutral unit and real GStreamer RTP tests.
    PYTHONPATH=/workspace/work/source python3 -m pytest \
      /workspace/work/source/test/test_artifact_manifest.py \
      /workspace/work/source/test/test_control_socket.py \
      /workspace/work/source/test/test_encoder.py \
      /workspace/work/source/test/test_frames.py \
      /workspace/work/source/test/test_media_edge_source_roster.py \
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
      # Only the isolated package prefix may enter the staging root.
      test -d install/ros_image_rtp_adapter
      mkdir -p /workspace/work/install-root/opt/ros/${ROS_DISTRO}
      rsync -a install/ros_image_rtp_adapter/ \
        /workspace/work/install-root/opt/ros/${ROS_DISTRO}/
    fi

    /workspace/repo/.xgc2/scripts/package_debs.sh \
      --install-root /workspace/work/install-root \
      --output-dir /workspace/out \
      --ros-distro "${ROS_DISTRO}"

    package_version="$(awk -F": *" "/^version:/ {print \$2; exit}" /workspace/repo/.xgc2/product.yml)"
    adapter_deb="/workspace/out/ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter_${package_version}_${actual_arch}.deb"
    test -f "${adapter_deb}"
    first_digest="$(sha256sum "${adapter_deb}" | awk "{print \$1}")"

    # Build again in a completely separate workspace and stage root. Matching
    # archives prove more than repacking the same tree twice: generated catkin/
    # colcon launchers, hooks and metadata must also be deterministic.
    mkdir -p /workspace/work/repro
    if [[ "${ROS_DISTRO}" == "noetic" ]]; then
      mkdir -p /workspace/work/repro/catkin/src
      ln -s /workspace/work/source/ros1 \
        /workspace/work/repro/catkin/src/ros_image_rtp_adapter
      cd /workspace/work/repro/catkin
      catkin_make \
        -DROS_IMAGE_RTP_COMMON_DIR=/workspace/work/source/ros_image_rtp_adapter
      DESTDIR=/workspace/work/repro/install-root catkin_make install \
        -DCMAKE_INSTALL_PREFIX=/opt/ros/noetic \
        -DROS_IMAGE_RTP_COMMON_DIR=/workspace/work/source/ros_image_rtp_adapter
    else
      mkdir -p /workspace/work/repro/src
      ln -s /workspace/work/source \
        /workspace/work/repro/src/ros_image_rtp_adapter
      cd /workspace/work/repro
      colcon build --packages-select ros_image_rtp_adapter
      test -d install/ros_image_rtp_adapter
      mkdir -p install-root/opt/ros/${ROS_DISTRO}
      rsync -a install/ros_image_rtp_adapter/ \
        install-root/opt/ros/${ROS_DISTRO}/
    fi
    mkdir -p /workspace/work/repro/out
    /workspace/repo/.xgc2/scripts/package_debs.sh \
      --install-root /workspace/work/repro/install-root \
      --output-dir /workspace/work/repro/out \
      --ros-distro "${ROS_DISTRO}"
    repro_deb="/workspace/work/repro/out/ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter_${package_version}_${actual_arch}.deb"
    test -f "${repro_deb}"
    second_digest="$(sha256sum "${repro_deb}" | awk "{print \$1}")"
    test "${first_digest}" = "${second_digest}"

    if [[ "${INSTALL_CHECK}" == "true" || "${RUN_INTEGRATION}" == "true" ]]; then
      apt-get install -y --no-install-recommends "${adapter_deb}"
    fi
    if [[ "${INSTALL_CHECK}" == "true" ]]; then
      # Do not use `head` under pipefail here: the Humble generated package file
      # list is large enough for dpkg to receive SIGPIPE after the reader exits.
      # sed consumes the complete list while keeping the install-check concise.
      dpkg -L ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter | sed -n "1,10p"
      if [[ "${ROS_DISTRO}" == "noetic" ]]; then
        bash -lc "source /opt/ros/noetic/setup.bash && rospack find ros_image_rtp_adapter"
      else
        bash -lc "source /opt/ros/${ROS_DISTRO}/setup.bash && ros2 pkg prefix ros_image_rtp_adapter"
      fi
    fi

    if [[ "${RUN_INTEGRATION}" == "true" ]]; then
      if [[ "${DEPENDENCY_MODE}" == "staging-apt" ]]; then
        echo "installing media-edge candidate from ${APT_OVERLAY_URL}"
        media_edge_policy="$(apt-cache policy xgc2-media-edge)"
        printf "%s\n" "${media_edge_policy}"
        media_edge_candidate="$(
          awk "/Candidate:/ {print \$2; exit}" <<<"${media_edge_policy}"
        )"
        if [[ -z "${media_edge_candidate}" || "${media_edge_candidate}" == "(none)" ]]; then
          echo "APT has no xgc2-media-edge candidate" >&2
          exit 1
        fi
        mapfile -t media_edge_uris < <(
          apt-get --print-uris download "xgc2-media-edge=${media_edge_candidate}" |
            sed -n "s#^[^h]*\(https://[A-Za-z0-9._~%+:/?=&-]*\).*#\1#p"
        )
        if [[ "${#media_edge_uris[@]}" -ne 1 ]]; then
          echo "expected one xgc2-media-edge candidate URI, found ${#media_edge_uris[@]}" >&2
          exit 1
        fi
        case "${media_edge_uris[0]}" in
          "${APT_OVERLAY_URL}"/*|https://xgc2.apt.xiaokang.ink/*) ;;
          *)
            echo "xgc2-media-edge candidate did not resolve from overlay or production APT" >&2
            exit 1
            ;;
        esac
        apt-get install -y --no-install-recommends \
          "xgc2-media-edge=${media_edge_candidate}"
        media_edge_source="${media_edge_uris[0]}"
      else
        echo "installing exact media-edge for integration (sha=${MEDIA_EDGE_SHA})"
        rm -rf /workspace/work/media-edge
        git init /workspace/work/media-edge
        git -C /workspace/work/media-edge remote add origin "${MEDIA_EDGE_URL}"
        git -C /workspace/work/media-edge fetch --depth 1 origin "${MEDIA_EDGE_SHA}"
        git -C /workspace/work/media-edge checkout --detach FETCH_HEAD
        test "$(git -C /workspace/work/media-edge rev-parse HEAD)" = "${MEDIA_EDGE_SHA}"
        test "$(awk -F": *" "/^version:/ {print \$2; exit}" /workspace/work/media-edge/.xgc2/product.yml)" = "${MEDIA_EDGE_VERSION}"

        rm -rf /workspace/work/media-edge-debs
        mkdir -p /workspace/work/media-edge-debs
        (
          cd /workspace/work/media-edge
          PACKAGE_DISTRIBUTION="${UBUNTU_CODENAME}" \
            XGC2_MEDIA_EDGE_DEB_OUTPUT_DIR=/workspace/work/media-edge-debs \
            ./.xgc2/scripts/build_deb.sh
        )
        apt-get install -y /workspace/work/media-edge-debs/xgc2-media-edge_*.deb
        media_edge_source="${MEDIA_EDGE_URL}@${MEDIA_EDGE_SHA}"
      fi

      media_edge_package_version="$(dpkg-query -W -f="\${Version}" xgc2-media-edge)"
      if [[ "${DEPENDENCY_MODE}" == "staging-apt" &&
            "${media_edge_package_version}" != "${media_edge_candidate}" ]]; then
        echo "installed media-edge ${media_edge_package_version} != candidate ${media_edge_candidate}" >&2
        exit 1
      fi
      test -x /usr/bin/xgc-media-edge
      test -x /usr/lib/xgc2-media-edge/mediamtx
      media_edge_binary_version="$(/usr/bin/xgc-media-edge --version)"
      if [[ -z "${media_edge_binary_version}" || "${media_edge_binary_version}" == "dev" ]]; then
        echo "installed media-edge binary has no immutable version" >&2
        exit 1
      fi
      case "${media_edge_package_version}" in
        "${media_edge_binary_version}~${UBUNTU_CODENAME}"|\
        "${media_edge_binary_version}+${UBUNTU_CODENAME}") ;;
        *)
          echo "media-edge package ${media_edge_package_version} does not bind binary ${media_edge_binary_version} to ${UBUNTU_CODENAME}" >&2
          exit 1
          ;;
      esac
      if [[ "${DEPENDENCY_MODE}" == "locked-source" &&
            "${media_edge_binary_version}" != "${MEDIA_EDGE_VERSION}" ]]; then
        echo "locked media-edge binary ${media_edge_binary_version} != ${MEDIA_EDGE_VERSION}" >&2
        exit 1
      fi

      MEDIA_EDGE_PACKAGE_VERSION="${media_edge_package_version}" \
      MEDIA_EDGE_SOURCE="${media_edge_source}" \
      DEPENDENCY_ARCHITECTURE="${actual_arch}" \
      python3 - <<PY
import json
import os
from pathlib import Path

receipt = {
    "schema": "xgc2.dependency-evidence.v1",
    "prepareAction": os.environ["PREPARE_ACTION"],
    "dependencySetDigest": os.environ["DEPENDENCY_SET_DIGEST"],
    "dependencyMode": os.environ["DEPENDENCY_MODE"],
    "distribution": os.environ["UBUNTU_CODENAME"],
    "architecture": os.environ["DEPENDENCY_ARCHITECTURE"],
    "dependencies": [
        {
            "package": "xgc2-media-edge",
            "version": os.environ["MEDIA_EDGE_PACKAGE_VERSION"],
            "source": os.environ["MEDIA_EDGE_SOURCE"],
        }
    ],
}
path = Path("/workspace/out/xgc2-dependency-evidence.json")
temporary = path.with_suffix(".json.tmp")
temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(path)
PY
      export MEDIA_EDGE_BINARY=/usr/bin/xgc-media-edge
      # repo is mounted read-only; copy scripts into the writable workdir
      cp -a /workspace/repo/scripts /workspace/work/integration-scripts
      chmod +x /workspace/work/integration-scripts/*.sh /workspace/work/integration-scripts/*.py
      cd /
      env -i \
        HOME=/root \
        LANG=C.UTF-8 \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        PYTHONNOUSERSITE=1 \
        EXPECTED_ADAPTER_PREFIX="/opt/ros/${ROS_DISTRO}" \
        MEDIA_EDGE_BINARY=/usr/bin/xgc-media-edge \
        ROS_DISTRO="${ROS_DISTRO}" \
        /bin/bash --noprofile --norc \
          /workspace/work/integration-scripts/integration_media_edge.sh
    fi
  '

echo "Debian package output:"
find "${OUTPUT_DIR}" -maxdepth 1 -type f -name "*.deb" -print | sort
