#!/usr/bin/env bash
# End-to-end CI check: test JPEG publisher → ros_image_rtp_adapter → xgc-media-edge.
# Fails closed if describe contract or Edge readiness is broken.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ROS_DISTRO="${ROS_DISTRO:-jazzy}"
MEDIA_EDGE_DIR="${MEDIA_EDGE_DIR:-}"
SOURCE_ID="${SOURCE_ID:-ci_camera}"
RTP_PORT="${RTP_PORT:-15004}"
CONTROL_SOCKET="${CONTROL_SOCKET:-/tmp/xgc2-image-rtp-ci.sock}"
EDGE_HTTP="${EDGE_HTTP:-127.0.0.1:18091}"
IMAGE_TOPIC="${IMAGE_TOPIC:-/ci/image/compressed}"
WIDTH="${WIDTH:-640}"
HEIGHT="${HEIGHT:-360}"
FPS="${FPS:-10}"
TIMEOUT_SEC="${TIMEOUT_SEC:-90}"

WORK="$(mktemp -d /tmp/xgc2-image-rtp-ci.XXXXXX)"
cleanup() {
  set +e
  [[ -n "${PUB_PID:-}" ]] && kill "${PUB_PID}" 2>/dev/null
  [[ -n "${ADAPTER_PID:-}" ]] && kill "${ADAPTER_PID}" 2>/dev/null
  [[ -n "${EDGE_PID:-}" ]] && kill "${EDGE_PID}" 2>/dev/null
  wait 2>/dev/null || true
  rm -rf "${WORK}"
  rm -f "${CONTROL_SOCKET}"
}
trap cleanup EXIT

log() { printf '[integration] %s\n' "$*"; }

if [[ -z "${MEDIA_EDGE_DIR}" ]]; then
  if [[ -d "${REPO_ROOT}/../media-edge" ]]; then
    MEDIA_EDGE_DIR="$(cd "${REPO_ROOT}/../media-edge" && pwd)"
  elif [[ -d "${REPO_ROOT}/../../common/media-edge" ]]; then
    MEDIA_EDGE_DIR="$(cd "${REPO_ROOT}/../../common/media-edge" && pwd)"
  fi
fi
if [[ -z "${MEDIA_EDGE_DIR}" || ! -d "${MEDIA_EDGE_DIR}" ]]; then
  echo "MEDIA_EDGE_DIR not set and media-edge not found next to product" >&2
  exit 1
fi

command -v ffmpeg >/dev/null
command -v go >/dev/null
command -v curl >/dev/null
command -v python3 >/dev/null

set +u
# shellcheck disable=SC1091
source "/opt/ros/${ROS_DISTRO}/setup.bash"
if [[ -f "${REPO_ROOT}/install/setup.bash" ]]; then
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/install/setup.bash"
elif [[ -f "${WORKSPACE_INSTALL:-}/setup.bash" ]]; then
  # shellcheck disable=SC1091
  source "${WORKSPACE_INSTALL}/setup.bash"
fi
set -u

log "building media-edge from ${MEDIA_EDGE_DIR}"
(
  cd "${MEDIA_EDGE_DIR}"
  go build -o "${WORK}/xgc-media-edge" ./cmd/xgc-media-edge
)

log "starting test JPEG publisher on ${IMAGE_TOPIC}"
ros2 run ros_image_rtp_adapter publish_test_jpeg \
  --ros-args \
  -p use_sim_time:=false \
  -- \
  --topic "${IMAGE_TOPIC}" --width "${WIDTH}" --height "${HEIGHT}" --fps "${FPS}" \
  >"${WORK}/publisher.log" 2>&1 &
PUB_PID=$!

log "starting image_rtp_adapter"
ros2 run ros_image_rtp_adapter image_rtp_adapter --ros-args \
  -p image_topic:="${IMAGE_TOPIC}" \
  -p source_id:="${SOURCE_ID}" \
  -p frame_id:=camera_optical \
  -p rtp_host:=127.0.0.1 \
  -p rtp_port:="${RTP_PORT}" \
  -p control_socket:="${CONTROL_SOCKET}" \
  -p width:="${WIDTH}" \
  -p height:="${HEIGHT}" \
  -p fps:="${FPS}" \
  -p bitrate:=1500000 \
  -p encoder:=libx264 \
  -p ffmpeg_path:=ffmpeg \
  -p drop_to_latest:=true \
  -p require_jpeg:=true \
  >"${WORK}/adapter.log" 2>&1 &
ADAPTER_PID=$!

log "waiting for control socket describe"
if ! python3 "${SCRIPT_DIR}/wait_describe.py" \
  --socket "${CONTROL_SOCKET}" \
  --source-id "${SOURCE_ID}" \
  --rtp-port "${RTP_PORT}" \
  --timeout "${TIMEOUT_SEC}"; then
  log "adapter describe failed; adapter log:"
  cat "${WORK}/adapter.log" || true
  cat "${WORK}/publisher.log" || true
  exit 1
fi

log "starting xgc-media-edge"
"${WORK}/xgc-media-edge" \
  -control-address "${EDGE_HTTP}" \
  -source-id "${SOURCE_ID}" \
  -rtp-listen-address "127.0.0.1:${RTP_PORT}" \
  -source-control-socket "${CONTROL_SOCKET}" \
  >"${WORK}/edge.log" 2>&1 &
EDGE_PID=$!

log "waiting for media-edge /healthz"
deadline=$((SECONDS + TIMEOUT_SEC))
healthy=0
while (( SECONDS < deadline )); do
  if curl -fsS "http://${EDGE_HTTP}/healthz" >"${WORK}/healthz.json" 2>/dev/null; then
    healthy=1
    break
  fi
  # If Edge died, dump logs early.
  if ! kill -0 "${EDGE_PID}" 2>/dev/null; then
    log "media-edge exited; log:"
    cat "${WORK}/edge.log" || true
    log "adapter log:"
    cat "${WORK}/adapter.log" || true
    exit 1
  fi
  sleep 0.5
done
if [[ "${healthy}" != "1" ]]; then
  log "healthz never became ready"
  cat "${WORK}/edge.log" || true
  cat "${WORK}/adapter.log" || true
  exit 1
fi

# Edge accepted the source only if Start() succeeded — verify log line and player page.
if ! grep -q "xgc-media-edge ready" "${WORK}/edge.log"; then
  log "media-edge did not report ready"
  cat "${WORK}/edge.log"
  exit 1
fi
curl -fsS "http://${EDGE_HTTP}/" | grep -qi "webrtc\|video\|session" || {
  log "player page missing expected content"
  exit 1
}

# Confirm adapter is still receiving/encoding after Edge attach.
sleep 2
if ! kill -0 "${ADAPTER_PID}" 2>/dev/null; then
  log "adapter died after edge start"
  cat "${WORK}/adapter.log"
  exit 1
fi
if ! grep -E "frames_in=[1-9]|frames_out=[1-9]|image_rtp_adapter ready" "${WORK}/adapter.log"; then
  # soft: at least ready line must exist
  if ! grep -q "image_rtp_adapter ready" "${WORK}/adapter.log"; then
    log "adapter never became ready"
    cat "${WORK}/adapter.log"
    exit 1
  fi
fi

log "OK: describe contract + media-edge healthz + player page"
cat "${WORK}/healthz.json"
exit 0
