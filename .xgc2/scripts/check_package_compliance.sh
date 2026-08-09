#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

test -f package.xml
test -f setup.py
test -f .xgc2/product.yml
test -f ros_image_rtp_adapter/node.py
test -f ros_image_rtp_adapter/ros1_node.py
test -f ros_image_rtp_adapter/runtime.py
test -f ros_image_rtp_adapter/settings.py
test -f ros_image_rtp_adapter/frames.py
test -f launch/image_rtp_adapter.launch.py
test -f config/jetson_thor_gstreamer.yaml
test -f config/jetson_nvmm_gstreamer.yaml
test -f ros1/package.xml
test -x ros1/scripts/image_rtp_adapter
test -f ros1/config/jetson_nvmm_gstreamer.yaml
test -f ros1/config/jetson_thor_gstreamer.yaml
test -f scripts/integration_media_edge.sh
test -f scripts/integration_gstreamer_rtp.py

version="$(awk -F': *' '/^version:[[:space:]]*/ {print $2; exit}' .xgc2/product.yml)"
test -n "${version}"
grep -Fq "<version>${version}</version>" package.xml
grep -Fq "version=\"${version}\"" setup.py
grep -Fq "version: ${version}" .xgc2/product-humble.yml
grep -Fq "version: ${version}" .xgc2/product-noetic.yml
grep -Fq "<version>${version}</version>" ros1/package.xml

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
python3 -m py_compile ros1/scripts/image_rtp_adapter ros1/scripts/publish_test_jpeg
bash -n scripts/integration_media_edge.sh scripts/lab_video_preview.sh

echo "compliance OK"
