#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

test -f package.xml
test -f setup.py
test -f .xgc2/product.yml
test -f ros_image_rtp_adapter/node.py
test -f launch/image_rtp_adapter.launch.py
test -f scripts/integration_media_edge.sh

# Topic must not be hard-coded to a brand path as the only option.
if grep -RIn --include='*.py' '/odin1/image' ros_image_rtp_adapter/node.py; then
  echo "node.py must not hard-code /odin1 topics; use parameters" >&2
  exit 1
fi

# Parameter declaration required
grep -q 'declare_parameter("image_topic"' ros_image_rtp_adapter/node.py
grep -q 'declare_parameter("source_id"' ros_image_rtp_adapter/node.py
grep -q 'declare_parameter("control_socket"' ros_image_rtp_adapter/node.py

python3 -m py_compile ros_image_rtp_adapter/control_socket.py
python3 -m py_compile ros_image_rtp_adapter/encoder.py
python3 -m py_compile ros_image_rtp_adapter/node.py
python3 -m py_compile ros_image_rtp_adapter/publish_test_jpeg.py

echo "compliance OK"
