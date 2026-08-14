#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT=""
OUTPUT_DIR=""
ROS_DISTRO="${ROS_DISTRO:-jazzy}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ROS_PACKAGE="ros_image_rtp_adapter"

product_version() {
  awk -F': *' '/^version:[[:space:]]*/ {print $2; exit}' \
    "${REPO_ROOT}/.xgc2/product.yml"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-root) INSTALL_ROOT="${2:?missing --install-root value}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:?missing --output-dir value}"; shift 2 ;;
    --ros-distro) ROS_DISTRO="${2:?missing --ros-distro value}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

case "${ROS_DISTRO}" in
  noetic) PYTHON_VERSION="3"; PYTHON_SITE="lib/python3/dist-packages" ;;
  humble) PYTHON_VERSION="3.10"; PYTHON_SITE="lib/python3.10/site-packages" ;;
  jazzy) PYTHON_VERSION="3.12"; PYTHON_SITE="lib/python3.12/site-packages" ;;
  *) echo "unsupported ROS distro: ${ROS_DISTRO}" >&2; exit 1 ;;
esac

VERSION="${PACKAGE_VERSION:-$(product_version)}"
if [[ ! "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "invalid package version: ${VERSION}" >&2
  exit 1
fi
if [[ ! "${SOURCE_DATE_EPOCH:-}" =~ ^[0-9]+$ ]]; then
  echo "SOURCE_DATE_EPOCH must be a non-negative integer" >&2
  exit 1
fi
for path in "${INSTALL_ROOT}" "${OUTPUT_DIR}"; do
  case "${path}" in
    /*) ;;
    *) echo "install root and output directory must be absolute" >&2; exit 1 ;;
  esac
done
INSTALL_ROOT="$(realpath -e -- "${INSTALL_ROOT}")"
OUTPUT_DIR="$(realpath -m -- "${OUTPUT_DIR}")"
if [[ "${INSTALL_ROOT}" == "/" || "${OUTPUT_DIR}" == "/" ]]; then
  echo "install root and output directory must not be /" >&2
  exit 1
fi
case "${INSTALL_ROOT}/" in "${OUTPUT_DIR}/"*) echo "install root must not be inside output directory" >&2; exit 1 ;; esac
case "${OUTPUT_DIR}/" in "${INSTALL_ROOT}/"*) echo "output directory must not be inside install root" >&2; exit 1 ;; esac

PACKAGE="ros-${ROS_DISTRO}-xgc2-ros-image-rtp-adapter"
ARCH="$(dpkg --print-architecture)"
PREFIX="/opt/ros/${ROS_DISTRO}"
PREFIX_ROOT="${INSTALL_ROOT}${PREFIX}"
PKG_ROOT="$(mktemp -d)"
trap 'rm -rf -- "${PKG_ROOT}"' EXIT

if [[ ! -d "${PREFIX_ROOT}" ]]; then
  echo "install root missing: ${PREFIX_ROOT}" >&2
  exit 1
fi
PREFIX_ROOT="$(realpath -e -- "${PREFIX_ROOT}")"
source_link="$(find -P "${INSTALL_ROOT}" -type l -print -quit)"
if [[ -n "${source_link}" ]]; then
  echo "install root contains a symbolic link: ${source_link}" >&2
  exit 1
fi
source_special="$(find -P "${INSTALL_ROOT}" ! -type d ! -type f -print -quit)"
if [[ -n "${source_special}" ]]; then
  echo "install root contains a non-regular entry: ${source_special}" >&2
  exit 1
fi

assert_source_beneath_prefix() {
  local source="$1"
  local resolved
  resolved="$(realpath -e -- "${source}")"
  case "${resolved}" in
    "${PREFIX_ROOT}"|"${PREFIX_ROOT}"/*) ;;
    *) echo "owned source escapes the ROS prefix: ${source}" >&2; exit 1 ;;
  esac
}

copy_owned_directory() {
  local relative="$1"
  local source="${PREFIX_ROOT}/${relative}"
  local target="${PKG_ROOT}${PREFIX}/${relative}"
  if [[ ! -d "${source}" || -L "${source}" ]]; then
    echo "required staged directory missing: ${source}" >&2
    exit 1
  fi
  assert_source_beneath_prefix "${source}"
  mkdir -p "${target}"
  rsync -a \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.pyo' \
    "${source}/" "${target}/"
}

copy_owned_file() {
  local relative="$1"
  local source="${PREFIX_ROOT}/${relative}"
  local target="${PKG_ROOT}${PREFIX}/${relative}"
  if [[ ! -f "${source}" || -L "${source}" ]]; then
    echo "required staged file missing: ${source}" >&2
    exit 1
  fi
  assert_source_beneath_prefix "${source}"
  install -D -m 0644 "${source}" "${target}"
}

# This is the complete package-owned runtime surface. Never copy a merged ROS
# prefix: doing so silently transfers ownership of other packages into this Deb.
copy_owned_directory "lib/${ROS_PACKAGE}"
copy_owned_directory "share/${ROS_PACKAGE}"
copy_owned_directory "${PYTHON_SITE}/${ROS_PACKAGE}"
if [[ "${ROS_DISTRO}" == "noetic" ]]; then
  copy_owned_file "lib/pkgconfig/${ROS_PACKAGE}.pc"

  # catkin writes source/devel paths into an unreachable branch of the
  # installed Config.cmake. They are still host-specific bytes and make the
  # package non-reproducible, so normalize exactly those generated fields to
  # the same empty values used by the live install branch. Refuse any shape we
  # do not recognize instead of broadly rewriting arbitrary CMake content.
  package_config="${PKG_ROOT}${PREFIX}/share/${ROS_PACKAGE}/cmake/${ROS_PACKAGE}Config.cmake"
  python3 - "${package_config}" "${PREFIX}" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
final_prefix = sys.argv[2]
content = path.read_text(encoding="utf-8")
source_pattern = re.compile(
    r"^  set\(ros_image_rtp_adapter_SOURCE_PREFIX (?P<value>/[^\r\n)]*)\)$",
    re.MULTILINE,
)
devel_pattern = re.compile(
    r"^  set\(ros_image_rtp_adapter_DEVEL_PREFIX (?P<value>/[^\r\n)]*)\)$",
    re.MULTILINE,
)
source_matches = list(source_pattern.finditer(content))
devel_matches = list(devel_pattern.finditer(content))
if len(source_matches) != 1 or not source_matches[0].group("value").endswith(
    "/catkin/src/ros_image_rtp_adapter"
):
    raise SystemExit("generated Config.cmake has an unexpected source prefix")
if len(devel_matches) != 1 or not devel_matches[0].group("value").endswith("/catkin/devel"):
    raise SystemExit("generated Config.cmake has an unexpected devel prefix")
install_line = f"  set(ros_image_rtp_adapter_INSTALL_PREFIX {final_prefix})"
if content.count(install_line) != 1:
    raise SystemExit("generated Config.cmake has an unexpected install prefix")
content = source_pattern.sub('  set(ros_image_rtp_adapter_SOURCE_PREFIX "")', content, count=1)
content = devel_pattern.sub('  set(ros_image_rtp_adapter_DEVEL_PREFIX "")', content, count=1)
path.write_text(content, encoding="utf-8")
PY
else
  copy_owned_file "share/ament_index/resource_index/packages/${ROS_PACKAGE}"
  copy_owned_file "share/colcon-core/packages/${ROS_PACKAGE}"
  for metadata_name in \
    PKG-INFO dependency_links.txt entry_points.txt requires.txt top_level.txt zip-safe; do
    copy_owned_file \
      "${PYTHON_SITE}/${ROS_PACKAGE}-${VERSION}-py${PYTHON_VERSION}.egg-info/${metadata_name}"
  done

  package_sh="${PKG_ROOT}${PREFIX}/share/${ROS_PACKAGE}/package.sh"
  python3 - "${package_sh}" "${PREFIX}" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
final_prefix = sys.argv[2]
content = path.read_text(encoding="utf-8")
pattern = re.compile(
    r'^_colcon_package_sh_COLCON_CURRENT_PREFIX="(?P<prefix>/[^"\n]+)"$',
    re.MULTILINE,
)
matches = list(pattern.finditer(content))
if len(matches) != 1 or not matches[0].group("prefix").endswith(
    "/install/ros_image_rtp_adapter"
):
    raise SystemExit("generated package.sh has an unexpected build prefix")
content = pattern.sub(
    '_colcon_package_sh_COLCON_CURRENT_PREFIX="%s"' % final_prefix,
    content,
    count=1,
)
path.write_text(content, encoding="utf-8")
PY
fi

if find "${PKG_ROOT}${PREFIX}" \( -type l -o -name '__pycache__' -o -name '*.py[co]' \) \
    -print -quit | grep -q .; then
  echo "staged package contains a symlink or Python bytecode cache" >&2
  find "${PKG_ROOT}${PREFIX}" \( -type l -o -name '__pycache__' -o -name '*.py[co]' \) \
    -print >&2
  exit 1
fi
if find "${PKG_ROOT}${PREFIX}" ! -type f ! -type d -print -quit | grep -q .; then
  echo "staged package contains a non-regular filesystem entry" >&2
  find "${PKG_ROOT}${PREFIX}" ! -type f ! -type d -print >&2
  exit 1
fi
workspace_leak_report="${PKG_ROOT}/.xgc2-workspace-path-leaks"
set +e
grep -RIl -- '/workspace/' "${PKG_ROOT}${PREFIX}" >"${workspace_leak_report}"
workspace_leak_status=$?
set -e
case "${workspace_leak_status}" in
  0)
    echo "staged package leaks a build workspace path" >&2
    cat "${workspace_leak_report}" >&2
    exit 1
    ;;
  1)
    rm -f -- "${workspace_leak_report}"
    ;;
  *)
    echo "cannot inspect staged package for build workspace paths" >&2
    exit "${workspace_leak_status}"
    ;;
esac

expected_owned_files() {
  if [[ "${ROS_DISTRO}" == "noetic" ]]; then
    cat <<EOF
lib/pkgconfig/${ROS_PACKAGE}.pc
lib/python3/dist-packages/${ROS_PACKAGE}/__init__.py
lib/python3/dist-packages/${ROS_PACKAGE}/control_socket.py
lib/python3/dist-packages/${ROS_PACKAGE}/encoder.py
lib/python3/dist-packages/${ROS_PACKAGE}/frames.py
lib/python3/dist-packages/${ROS_PACKAGE}/node.py
lib/python3/dist-packages/${ROS_PACKAGE}/publish_test_jpeg.py
lib/python3/dist-packages/${ROS_PACKAGE}/ros1_node.py
lib/python3/dist-packages/${ROS_PACKAGE}/runtime.py
lib/python3/dist-packages/${ROS_PACKAGE}/settings.py
lib/${ROS_PACKAGE}/image_rtp_adapter
lib/${ROS_PACKAGE}/publish_test_jpeg
share/${ROS_PACKAGE}/cmake/${ROS_PACKAGE}Config-version.cmake
share/${ROS_PACKAGE}/cmake/${ROS_PACKAGE}Config.cmake
share/${ROS_PACKAGE}/config/default.yaml
share/${ROS_PACKAGE}/config/jetson_nvmm_gstreamer.yaml
share/${ROS_PACKAGE}/launch/image_rtp_adapter.launch
share/${ROS_PACKAGE}/package.xml
EOF
    return
  fi
  cat <<EOF
${PYTHON_SITE}/${ROS_PACKAGE}-${VERSION}-py${PYTHON_VERSION}.egg-info/PKG-INFO
${PYTHON_SITE}/${ROS_PACKAGE}-${VERSION}-py${PYTHON_VERSION}.egg-info/dependency_links.txt
${PYTHON_SITE}/${ROS_PACKAGE}-${VERSION}-py${PYTHON_VERSION}.egg-info/entry_points.txt
${PYTHON_SITE}/${ROS_PACKAGE}-${VERSION}-py${PYTHON_VERSION}.egg-info/requires.txt
${PYTHON_SITE}/${ROS_PACKAGE}-${VERSION}-py${PYTHON_VERSION}.egg-info/top_level.txt
${PYTHON_SITE}/${ROS_PACKAGE}-${VERSION}-py${PYTHON_VERSION}.egg-info/zip-safe
${PYTHON_SITE}/${ROS_PACKAGE}/__init__.py
${PYTHON_SITE}/${ROS_PACKAGE}/control_socket.py
${PYTHON_SITE}/${ROS_PACKAGE}/encoder.py
${PYTHON_SITE}/${ROS_PACKAGE}/frames.py
${PYTHON_SITE}/${ROS_PACKAGE}/node.py
${PYTHON_SITE}/${ROS_PACKAGE}/publish_test_jpeg.py
${PYTHON_SITE}/${ROS_PACKAGE}/ros1_node.py
${PYTHON_SITE}/${ROS_PACKAGE}/runtime.py
${PYTHON_SITE}/${ROS_PACKAGE}/settings.py
lib/${ROS_PACKAGE}/image_rtp_adapter
lib/${ROS_PACKAGE}/publish_test_jpeg
share/ament_index/resource_index/packages/${ROS_PACKAGE}
share/colcon-core/packages/${ROS_PACKAGE}
share/${ROS_PACKAGE}/config/default.yaml
share/${ROS_PACKAGE}/config/jetson_nvmm_gstreamer.yaml
share/${ROS_PACKAGE}/hook/ament_prefix_path.dsv
share/${ROS_PACKAGE}/hook/ament_prefix_path.ps1
share/${ROS_PACKAGE}/hook/ament_prefix_path.sh
share/${ROS_PACKAGE}/hook/pythonpath.dsv
share/${ROS_PACKAGE}/hook/pythonpath.ps1
share/${ROS_PACKAGE}/hook/pythonpath.sh
share/${ROS_PACKAGE}/launch/image_rtp_adapter.launch.py
share/${ROS_PACKAGE}/package.bash
share/${ROS_PACKAGE}/package.dsv
share/${ROS_PACKAGE}/package.ps1
share/${ROS_PACKAGE}/package.sh
share/${ROS_PACKAGE}/package.xml
share/${ROS_PACKAGE}/package.zsh
EOF
}

actual_files="$(find "${PKG_ROOT}${PREFIX}" -type f -printf '%P\n' | LC_ALL=C sort)"
expected_files="$(expected_owned_files | LC_ALL=C sort)"
if [[ "${actual_files}" != "${expected_files}" ]]; then
  echo "staged package does not match the exact ${ROS_DISTRO} ownership manifest" >&2
  diff -u <(printf '%s\n' "${expected_files}") \
    <(printf '%s\n' "${actual_files}") >&2
  exit 1
fi

test -f "${PKG_ROOT}${PREFIX}/${PYTHON_SITE}/${ROS_PACKAGE}/node.py"
test -f "${PKG_ROOT}${PREFIX}/${PYTHON_SITE}/${ROS_PACKAGE}/runtime.py"
test -x "${PKG_ROOT}${PREFIX}/lib/${ROS_PACKAGE}/image_rtp_adapter"

mkdir -p "${OUTPUT_DIR}" "${PKG_ROOT}/DEBIAN" \
  "${PKG_ROOT}/usr/share/doc/${PACKAGE}"

if [[ "${ROS_DISTRO}" == "noetic" ]]; then
  DEPENDS="ros-noetic-rospy, ros-noetic-sensor-msgs, ffmpeg, libavcodec-extra, python3-numpy, python3-pil, gstreamer1.0-tools, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-plugins-ugly"
else
  DEPENDS="ros-${ROS_DISTRO}-rclpy, ros-${ROS_DISTRO}-sensor-msgs, ros-${ROS_DISTRO}-std-msgs, ros-${ROS_DISTRO}-launch, ros-${ROS_DISTRO}-launch-ros, ffmpeg, libavcodec-extra, python3-numpy, python3-pil, gstreamer1.0-tools, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-plugins-ugly"
fi

cat >"${PKG_ROOT}/DEBIAN/control" <<EOF
Package: ${PACKAGE}
Version: ${VERSION}
Section: misc
Priority: optional
Architecture: ${ARCH}
Maintainer: XGC2 <apt@xgc2.local>
Depends: ${DEPENDS}
Recommends: xgc2-media-edge
Description: XGC2 ROS image to media-edge H264/RTP adapter
 Parameterized bridge from sensor_msgs/Image or CompressedImage to the
 xgc2-media-edge source contract with configurable FFmpeg or GStreamer
 encoder backends.
EOF

printf '%s\n' "${PACKAGE}" >"${PKG_ROOT}/usr/share/doc/${PACKAGE}/README"
cat >"${PKG_ROOT}/usr/share/doc/${PACKAGE}/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: ros_image_rtp_adapter
Source: https://github.com/lxk36/xgc2-ros-image-rtp-adapter

Files: *
Copyright: 2026 XGC2 contributors
License: Apache-2.0
 On Debian systems, the complete Apache License 2.0 text is available at
 /usr/share/common-licenses/Apache-2.0.
EOF
find "${PKG_ROOT}" -type d -exec chmod 0755 {} +
find "${PKG_ROOT}" -type f -exec chmod 0644 {} +
find "${PKG_ROOT}${PREFIX}/lib/${ROS_PACKAGE}" -type f -exec chmod 0755 {} +
chmod 0755 "${PKG_ROOT}/DEBIAN"

# dpkg-deb honors SOURCE_DATE_EPOCH. Normalize the entire tree as an explicit
# additional fence so source/build mtimes and Python imports cannot perturb it.
find "${PKG_ROOT}" -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

DEB="${OUTPUT_DIR}/${PACKAGE}_${VERSION}_${ARCH}.deb"
if [[ -e "${DEB}" || -L "${DEB}" ]]; then
  echo "refusing to overwrite existing package output: ${DEB}" >&2
  exit 1
fi
export SOURCE_DATE_EPOCH
dpkg-deb --root-owner-group --uniform-compression -Zxz -z9 \
  --build "${PKG_ROOT}" "${DEB}" >/dev/null

size="$(stat -c%s "${DEB}")"
if (( size < 8000 )); then
  echo "deb package too small (${size} bytes); install layout incomplete" >&2
  dpkg-deb -c "${DEB}" >&2
  exit 1
fi
echo "built ${DEB} (${size} bytes)"
printf '%s\n' "${DEB}"
