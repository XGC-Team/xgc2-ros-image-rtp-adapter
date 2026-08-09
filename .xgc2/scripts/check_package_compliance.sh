#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

test -f package.xml
test -f setup.py
test -f .xgc2/product.yml
test -f ros_image_rtp_adapter/node.py
test -f launch/image_rtp_adapter.launch.py
test -f config/jetson_thor_gstreamer.yaml
test -f scripts/integration_media_edge.sh
test -f scripts/integration_gstreamer_rtp.py

version="$(awk -F': *' '/^version:[[:space:]]*/ {print $2; exit}' .xgc2/product.yml)"
test -n "${version}"
grep -Fq "<version>${version}</version>" package.xml
grep -Fq "version=\"${version}\"" setup.py
grep -Fq "version: ${version}" .xgc2/product-humble.yml

# Topic must not be hard-coded to a brand path as the only option.
if grep -RIn --include='*.py' '/odin1/image' ros_image_rtp_adapter/node.py; then
  echo "node.py must not hard-code /odin1 topics; use parameters" >&2
  exit 1
fi

# Parameter declaration required
grep -q 'declare_parameter("image_topic"' ros_image_rtp_adapter/node.py
grep -q 'declare_parameter("source_id"' ros_image_rtp_adapter/node.py
grep -q 'declare_parameter("control_socket"' ros_image_rtp_adapter/node.py
grep -q 'declare_parameter("encoder_backend"' ros_image_rtp_adapter/node.py

# Vendor element factories belong in opt-in deployment profiles, never in the
# runtime implementation. This keeps the package usable on non-NVIDIA devices.
if grep -RInE --include='*.py' 'nvjpegdec|nvvidconv|nvv4l2h264enc|Jetson|Thor' \
    ros_image_rtp_adapter; then
  echo "runtime Python must not hard-code a vendor or board profile" >&2
  exit 1
fi

python3 -m py_compile ros_image_rtp_adapter/control_socket.py
python3 -m py_compile ros_image_rtp_adapter/encoder.py
python3 -m py_compile ros_image_rtp_adapter/node.py
python3 -m py_compile ros_image_rtp_adapter/publish_test_jpeg.py
python3 -m py_compile launch/image_rtp_adapter.launch.py
python3 -m py_compile scripts/integration_gstreamer_rtp.py
bash -n scripts/integration_media_edge.sh scripts/lab_video_preview.sh

echo "compliance OK"
