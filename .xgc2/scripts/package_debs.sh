#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT=""
OUTPUT_DIR=""
ROS_DISTRO="${ROS_DISTRO:-jazzy}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ROS_PACKAGE="ros_image_rtp_adapter"

product_version() {
  awk -F': *' '/^version:[[:space:]]*/ {print $2; exit}' "${product_file}"
}

PACKAGE="ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-root) INSTALL_ROOT="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --ros-distro) ROS_DISTRO="$2"; PACKAGE="ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

product_file="${REPO_ROOT}/.xgc2/product.yml"
if [[ "${ROS_DISTRO}" == "humble" ]]; then
  product_file="${REPO_ROOT}/.xgc2/product-humble.yml"
elif [[ "${ROS_DISTRO}" == "noetic" ]]; then
  product_file="${REPO_ROOT}/.xgc2/product-noetic.yml"
fi
VERSION="${PACKAGE_VERSION:-$(product_version)}"

if [[ -z "${INSTALL_ROOT}" || -z "${OUTPUT_DIR}" || -z "${VERSION}" ]]; then
  echo "missing required args or version" >&2
  exit 1
fi

ARCH="$(dpkg --print-architecture)"
PREFIX="/opt/ros/${ROS_DISTRO}"
PREFIX_ROOT="${INSTALL_ROOT}${PREFIX}"
PKG_ROOT="$(mktemp -d)"
trap 'rm -rf "${PKG_ROOT}"' EXIT

mkdir -p "${OUTPUT_DIR}" "${PKG_ROOT}/DEBIAN" "${PKG_ROOT}/usr/share/doc/${PACKAGE}"
rm -f "${OUTPUT_DIR}"/${PACKAGE}_*.deb

if [[ ! -d "${PREFIX_ROOT}" ]]; then
  echo "install root missing: ${PREFIX_ROOT}" >&2
  exit 1
fi

# Copy the full staged ROS prefix install for this package (ament_python layout).
mkdir -p "${PKG_ROOT}${PREFIX}"
# Prefer isolated package install; otherwise merged install tree.
if [[ -d "${PREFIX_ROOT}/lib/${ROS_PACKAGE}" || -d "${PREFIX_ROOT}/share/${ROS_PACKAGE}" ]]; then
  rsync -a \
    --include "lib/" \
    --include "lib/${ROS_PACKAGE}/***" \
    --include "lib/python*/***" \
    --include "share/" \
    --include "share/${ROS_PACKAGE}/***" \
    --include "share/ament_index/***" \
    --include "local/***" \
    --exclude "*" \
    "${PREFIX_ROOT}/" "${PKG_ROOT}${PREFIX}/" || true
  # Also full rsync of known trees if filter missed python path variants
  [[ -d "${PREFIX_ROOT}/lib" ]] && rsync -a "${PREFIX_ROOT}/lib/" "${PKG_ROOT}${PREFIX}/lib/"
  [[ -d "${PREFIX_ROOT}/share" ]] && rsync -a "${PREFIX_ROOT}/share/" "${PKG_ROOT}${PREFIX}/share/"
else
  rsync -a "${PREFIX_ROOT}/" "${PKG_ROOT}${PREFIX}/"
fi

# Sanity: must ship the entrypoint and at least one python module.
if ! find "${PKG_ROOT}${PREFIX}" -type f -name 'image_rtp_adapter' | grep -q .; then
  echo "entrypoint image_rtp_adapter missing from staged package" >&2
  find "${PKG_ROOT}${PREFIX}" | head -80 >&2 || true
  exit 1
fi
if ! find "${PKG_ROOT}${PREFIX}" -type f -name 'node.py' | grep -q .; then
  echo "python module node.py missing from staged package" >&2
  find "${PKG_ROOT}${PREFIX}" | head -80 >&2 || true
  exit 1
fi

if [[ "${ROS_DISTRO}" == "noetic" ]]; then
  DEPENDS="ros-noetic-rospy, ros-noetic-sensor-msgs, ffmpeg, python3-pil, gstreamer1.0-tools, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-plugins-ugly"
else
  DEPENDS="ros-${ROS_DISTRO}-rclpy, ros-${ROS_DISTRO}-sensor-msgs, ros-${ROS_DISTRO}-std-msgs, ros-${ROS_DISTRO}-launch, ros-${ROS_DISTRO}-launch-ros, ffmpeg, python3-numpy, python3-pil, gstreamer1.0-tools, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-plugins-ugly"
fi

cat >"${PKG_ROOT}/DEBIAN/control" <<EOF
Package: ${PACKAGE}
Version: ${VERSION}
Section: misc
Priority: optional
Architecture: ${ARCH}
Maintainer: XGC2 <apt@xgc2.local>
Depends: ${DEPENDS}
Description: XGC2 ROS image to media-edge H264/RTP adapter
 Parameterized bridge from sensor_msgs/Image or CompressedImage to the
 xgc2-media-edge source contract with configurable FFmpeg or GStreamer
 encoder backends.
EOF

printf '%s\n' "${PACKAGE}" >"${PKG_ROOT}/usr/share/doc/${PACKAGE}/README"
find "${PKG_ROOT}" -type d -exec chmod 0755 {} +
find "${PKG_ROOT}" -type f -exec chmod 0644 {} +
if [[ -d "${PKG_ROOT}${PREFIX}/lib/${ROS_PACKAGE}" ]]; then
  find "${PKG_ROOT}${PREFIX}/lib/${ROS_PACKAGE}" -type f -exec chmod 0755 {} +
fi
chmod 0755 "${PKG_ROOT}/DEBIAN"

fakeroot dpkg-deb --build "${PKG_ROOT}" "${OUTPUT_DIR}/${PACKAGE}_${VERSION}_${ARCH}.deb" >/dev/null
DEB="$(find "${OUTPUT_DIR}" -maxdepth 1 -type f -name "${PACKAGE}_*.deb" | sort | tail -1)"
echo "built ${DEB} ($(stat -c%s "${DEB}") bytes)"
# Fail closed on empty-ish packages.
size="$(stat -c%s "${DEB}")"
# Pure-Python ament package is intentionally small; require a real layout, not empty shell.
if (( size < 8000 )); then
  echo "deb package too small (${size} bytes); install layout incomplete" >&2
  dpkg-deb -c "${DEB}" | head -100 >&2 || true
  exit 1
fi
find "${OUTPUT_DIR}" -maxdepth 1 -type f -name "${PACKAGE}_*.deb" -print | sort
