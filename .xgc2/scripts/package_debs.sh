#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT=""
OUTPUT_DIR=""
ROS_DISTRO="${ROS_DISTRO:-jazzy}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ROS_PACKAGE="ros_image_rtp_adapter"

product_file="${REPO_ROOT}/.xgc2/product.yml"
if [[ "${ROS_DISTRO}" == "humble" && -f "${REPO_ROOT}/.xgc2/product-humble.yml" ]]; then
  product_file="${REPO_ROOT}/.xgc2/product-humble.yml"
fi

product_version() {
  awk -F': *' '/^version:[[:space:]]*/ {print $2; exit}' "${product_file}"
}

VERSION="${PACKAGE_VERSION:-$(product_version)}"
PACKAGE="ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-root) INSTALL_ROOT="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --ros-distro) ROS_DISTRO="$2"; PACKAGE="ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

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

if [[ ! -d "${PREFIX_ROOT}/share/${ROS_PACKAGE}" && ! -d "${PREFIX_ROOT}/lib/${ROS_PACKAGE}" ]]; then
  echo "staged package missing under ${PREFIX_ROOT}" >&2
  find "${PREFIX_ROOT}" -maxdepth 3 -type d 2>/dev/null | head -50 || true
  exit 1
fi

mkdir -p "${PKG_ROOT}${PREFIX}"
# Copy entire ROS prefix install for this package tree.
if [[ -d "${PREFIX_ROOT}/share/${ROS_PACKAGE}" ]]; then
  mkdir -p "${PKG_ROOT}${PREFIX}/share"
  cp -a "${PREFIX_ROOT}/share/${ROS_PACKAGE}" "${PKG_ROOT}${PREFIX}/share/"
fi
if [[ -d "${PREFIX_ROOT}/lib/${ROS_PACKAGE}" ]]; then
  mkdir -p "${PKG_ROOT}${PREFIX}/lib"
  cp -a "${PREFIX_ROOT}/lib/${ROS_PACKAGE}" "${PKG_ROOT}${PREFIX}/lib/"
fi
if [[ -d "${PREFIX_ROOT}/lib/python3" ]]; then
  mkdir -p "${PKG_ROOT}${PREFIX}/lib"
  cp -a "${PREFIX_ROOT}/lib/python3" "${PKG_ROOT}${PREFIX}/lib/" 2>/dev/null || true
fi
# ament index
AMENT_RESOURCE_ROOT="${PREFIX_ROOT}/share/ament_index/resource_index"
if [[ -d "${AMENT_RESOURCE_ROOT}" ]]; then
  while IFS= read -r -d '' resource; do
    relative="${resource#${INSTALL_ROOT}}"
    mkdir -p "${PKG_ROOT}$(dirname "${relative}")"
    cp -a "${resource}" "${PKG_ROOT}${relative}"
  done < <(find "${AMENT_RESOURCE_ROOT}" -type f -name "${ROS_PACKAGE}" -print0 2>/dev/null || true)
fi

# Also copy local site-packages layout if present
if [[ -d "${PREFIX_ROOT}/local" ]]; then
  cp -a "${PREFIX_ROOT}/local" "${PKG_ROOT}${PREFIX}/" 2>/dev/null || true
fi

DEPENDS="ros-${ROS_DISTRO}-rclpy, ros-${ROS_DISTRO}-sensor-msgs, ros-${ROS_DISTRO}-std-msgs, ros-${ROS_DISTRO}-launch, ros-${ROS_DISTRO}-launch-ros, ffmpeg, python3-numpy"

cat >"${PKG_ROOT}/DEBIAN/control" <<EOF
Package: ${PACKAGE}
Version: ${VERSION}
Section: misc
Priority: optional
Architecture: ${ARCH}
Maintainer: XGC2 <apt@xgc2.local>
Depends: ${DEPENDS}
Description: XGC2 ROS CompressedImage JPEG to media-edge H264/RTP adapter
 Parameterized bridge from sensor_msgs/CompressedImage to the xgc2-media-edge
 source contract (H264/RTP + Unix control socket).
EOF

printf '%s\n' "${PACKAGE}" >"${PKG_ROOT}/usr/share/doc/${PACKAGE}/README"
find "${PKG_ROOT}" -type d -exec chmod 0755 {} +
find "${PKG_ROOT}" -type f -exec chmod 0644 {} +
# executables
if [[ -d "${PKG_ROOT}${PREFIX}/lib/${ROS_PACKAGE}" ]]; then
  find "${PKG_ROOT}${PREFIX}/lib/${ROS_PACKAGE}" -type f -exec chmod 0755 {} +
fi
chmod 0755 "${PKG_ROOT}/DEBIAN"

fakeroot dpkg-deb --build "${PKG_ROOT}" "${OUTPUT_DIR}/${PACKAGE}_${VERSION}_${ARCH}.deb" >/dev/null
find "${OUTPUT_DIR}" -maxdepth 1 -type f -name "${PACKAGE}_*.deb" -print | sort
