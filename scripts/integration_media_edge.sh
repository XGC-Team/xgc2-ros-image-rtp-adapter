#!/usr/bin/env bash
# shellcheck disable=SC1090 # ROS distro setup path is selected by the matrix.
# End-to-end CI check: test JPEG publisher → ros_image_rtp_adapter → xgc-media-edge.
# Fails closed if describe contract or Edge readiness is broken.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ROS_DISTRO="${ROS_DISTRO:-jazzy}"
EXPECTED_ADAPTER_PREFIX="${EXPECTED_ADAPTER_PREFIX:-/opt/ros/${ROS_DISTRO}}"
MEDIA_EDGE_DIR="${MEDIA_EDGE_DIR:-}"
MEDIA_EDGE_BINARY="${MEDIA_EDGE_BINARY:-}"
SOURCE_ID="${SOURCE_ID:-ci_camera}"
RTP_PORT="${RTP_PORT:-15004}"
EDGE_HTTP="${EDGE_HTTP:-127.0.0.1:18091}"
IMAGE_TOPIC="${IMAGE_TOPIC:-/ci/image/compressed}"
WIDTH="${WIDTH:-640}"
HEIGHT="${HEIGHT:-360}"
FPS="${FPS:-10}"
TIMEOUT_SEC="${TIMEOUT_SEC:-90}"

case "${ROS_DISTRO}" in
  noetic|humble|jazzy) ;;
  *) echo "unsupported ROS distro: ${ROS_DISTRO}" >&2; exit 1 ;;
esac

if [[ -n "${CONTROL_SOCKET+x}" ]]; then
  echo "CONTROL_SOCKET is run-owned and cannot be overridden" >&2
  exit 1
fi
cleanup() {
  set +e
  [[ -n "${PUB_PID:-}" ]] && kill "${PUB_PID}" 2>/dev/null
  [[ -n "${ADAPTER_PID:-}" ]] && kill "${ADAPTER_PID}" 2>/dev/null
  [[ -n "${EDGE_PID:-}" ]] && kill "${EDGE_PID}" 2>/dev/null
  [[ -n "${MASTER_PID:-}" ]] && kill "${MASTER_PID}" 2>/dev/null
  wait 2>/dev/null || true
  [[ -n "${WORK:-}" ]] && rm -rf -- "${WORK}"
}
WORK="$(mktemp -d /tmp/xgc2-image-rtp-ci.XXXXXX)"
trap cleanup EXIT
CONTROL_SOCKET="${WORK}/adapter-control.sock"
for integer_contract in \
  "RTP_PORT:${RTP_PORT}:1:65535" \
  "WIDTH:${WIDTH}:1:16384" \
  "HEIGHT:${HEIGHT}:1:16384" \
  "FPS:${FPS}:1:240" \
  "TIMEOUT_SEC:${TIMEOUT_SEC}:1:600"; do
  IFS=: read -r integer_name integer_value integer_min integer_max \
    <<<"${integer_contract}"
  if [[ ! "${integer_value}" =~ ^[0-9]+$ ]] || \
      (( 10#${integer_value} < integer_min || 10#${integer_value} > integer_max )); then
    echo "${integer_name} must be an integer in [${integer_min}, ${integer_max}]" >&2
    exit 1
  fi
done

log() { printf '[integration] %s\n' "$*"; }

if [[ -z "${MEDIA_EDGE_DIR}" ]]; then
  if [[ -d "${REPO_ROOT}/../media-edge" ]]; then
    MEDIA_EDGE_DIR="$(cd "${REPO_ROOT}/../media-edge" && pwd)"
  elif [[ -d "${REPO_ROOT}/../../common/media-edge" ]]; then
    MEDIA_EDGE_DIR="$(cd "${REPO_ROOT}/../../common/media-edge" && pwd)"
  fi
fi
if [[ -z "${MEDIA_EDGE_BINARY}" && ( -z "${MEDIA_EDGE_DIR}" || ! -d "${MEDIA_EDGE_DIR}" ) ]]; then
  echo "MEDIA_EDGE_DIR not set and media-edge not found next to product" >&2
  exit 1
fi
if [[ -n "${MEDIA_EDGE_BINARY}" && ! -x "${MEDIA_EDGE_BINARY}" ]]; then
  echo "MEDIA_EDGE_BINARY is not executable: ${MEDIA_EDGE_BINARY}" >&2
  exit 1
fi

command -v ffmpeg >/dev/null
if [[ -z "${MEDIA_EDGE_BINARY}" ]]; then
  command -v go >/dev/null
fi
command -v curl >/dev/null
command -v python3 >/dev/null

set +u
# shellcheck disable=SC1091
source "/opt/ros/${ROS_DISTRO}/setup.bash"
set -u

case "${EXPECTED_ADAPTER_PREFIX}" in
  /*) ;;
  *) echo "EXPECTED_ADAPTER_PREFIX must be absolute" >&2; exit 1 ;;
esac
if [[ "${ROS_DISTRO}" == "noetic" ]]; then
  actual_adapter_prefix="$(rospack find ros_image_rtp_adapter)"
  expected_adapter_prefix="${EXPECTED_ADAPTER_PREFIX}/share/ros_image_rtp_adapter"
else
  actual_adapter_prefix="$(ros2 pkg prefix ros_image_rtp_adapter)"
  expected_adapter_prefix="${EXPECTED_ADAPTER_PREFIX}"
fi
if [[ "${actual_adapter_prefix}" != "${expected_adapter_prefix}" ]]; then
  echo "adapter resolved from ${actual_adapter_prefix}, expected ${expected_adapter_prefix}" >&2
  exit 1
fi
EXPECTED_ADAPTER_PREFIX="${EXPECTED_ADAPTER_PREFIX}" python3 - <<'PY'
import os
from pathlib import Path
import ros_image_rtp_adapter

module_path = Path(ros_image_rtp_adapter.__file__).resolve()
expected_prefix = Path(os.environ["EXPECTED_ADAPTER_PREFIX"]).resolve()
if expected_prefix not in module_path.parents:
    raise SystemExit(
        "adapter Python module resolved from %s, expected beneath %s"
        % (module_path, expected_prefix)
    )
PY

if [[ -n "${MEDIA_EDGE_BINARY}" ]]; then
  log "using media-edge binary ${MEDIA_EDGE_BINARY}"
  cp "${MEDIA_EDGE_BINARY}" "${WORK}/xgc-media-edge"
else
  log "building media-edge from ${MEDIA_EDGE_DIR}"
  (
    cd "${MEDIA_EDGE_DIR}"
    go build -o "${WORK}/xgc-media-edge" ./cmd/xgc-media-edge
  )
fi

if [[ "${ROS_DISTRO}" == "noetic" ]]; then
  log "starting ROS 1 master"
  roscore >"${WORK}/roscore.log" 2>&1 &
  MASTER_PID=$!
  deadline=$((SECONDS + TIMEOUT_SEC))
  until rosparam list >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      cat "${WORK}/roscore.log" >&2 || true
      exit 1
    fi
    sleep 0.25
  done

  log "starting ROS 1 test JPEG publisher on ${IMAGE_TOPIC}"
  rosrun ros_image_rtp_adapter publish_test_jpeg \
    --topic "${IMAGE_TOPIC}" --width "${WIDTH}" --height "${HEIGHT}" --fps "${FPS}" \
    >"${WORK}/publisher.log" 2>&1 &
  PUB_PID=$!

  log "starting ROS 1 image_rtp_adapter"
  rosrun ros_image_rtp_adapter image_rtp_adapter \
    _image_topic:="${IMAGE_TOPIC}" \
    _input_message_type:=compressed \
    _source_id:="${SOURCE_ID}" \
    _frame_id:=camera_optical \
    _rtp_host:=127.0.0.1 \
    _rtp_port:="${RTP_PORT}" \
    _control_socket:="${CONTROL_SOCKET}" \
    _width:="${WIDTH}" \
    _height:="${HEIGHT}" \
    _fps:="${FPS}.0" \
    _bitrate:=1500000 \
    _encoder:=libx264 \
    _ffmpeg_path:=ffmpeg \
    _drop_to_latest:=true \
    _require_jpeg:=true \
    >"${WORK}/adapter.log" 2>&1 &
  ADAPTER_PID=$!
else
  log "starting ROS 2 test JPEG publisher on ${IMAGE_TOPIC}"
  # Args before --ros-args so publish_test_jpeg argparse receives them.
  ros2 run ros_image_rtp_adapter publish_test_jpeg \
    --topic "${IMAGE_TOPIC}" --width "${WIDTH}" --height "${HEIGHT}" --fps "${FPS}" \
    >"${WORK}/publisher.log" 2>&1 &
  PUB_PID=$!

  log "starting ROS 2 image_rtp_adapter"
  ros2 run ros_image_rtp_adapter image_rtp_adapter --ros-args \
    -p image_topic:="${IMAGE_TOPIC}" \
    -p input_message_type:=compressed \
    -p source_id:="${SOURCE_ID}" \
    -p frame_id:=camera_optical \
    -p rtp_host:=127.0.0.1 \
    -p rtp_port:="${RTP_PORT}" \
    -p control_socket:="${CONTROL_SOCKET}" \
    -p width:="${WIDTH}" \
    -p height:="${HEIGHT}" \
    -p fps:="${FPS}.0" \
    -p bitrate:=1500000 \
    -p encoder:=libx264 \
    -p ffmpeg_path:=ffmpeg \
    -p drop_to_latest:=true \
    -p require_jpeg:=true \
    >"${WORK}/adapter.log" 2>&1 &
  ADAPTER_PID=$!
fi

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

SOURCES_CONFIG="${WORK}/media-edge-sources.json"
python3 "${SCRIPT_DIR}/write_media_edge_source_roster.py" \
  --output "${SOURCES_CONFIG}" \
  --source-id "${SOURCE_ID}" \
  --rtp-port "${RTP_PORT}" \
  --control-socket "${CONTROL_SOCKET}"

log "starting xgc-media-edge"
"${WORK}/xgc-media-edge" \
  -control-address "${EDGE_HTTP}" \
  -sources-config "${SOURCES_CONFIG}" \
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

# Edge accepted the source only if Start() succeeded. Verify the embedded
# player's structural contract and executable assets; visible copy belongs to
# the React application and is not an integration API.
if ! grep -q "xgc-media-edge ready" "${WORK}/edge.log"; then
  log "media-edge did not report ready"
  cat "${WORK}/edge.log"
  exit 1
fi
curl -fsS "http://${EDGE_HTTP}/" >"${WORK}/player.html"
if ! grep -Fq 'id="app" data-source-id=' "${WORK}/player.html" || \
    ! grep -Fq 'href="/assets/player.css"' "${WORK}/player.html" || \
    ! grep -Fq 'type="module" src="/assets/player.js"' "${WORK}/player.html"; then
  log "player shell is missing its source mount or immutable assets"
  exit 1
fi
curl -fsS "http://${EDGE_HTTP}/assets/player.js" >"${WORK}/player.js"
curl -fsS "http://${EDGE_HTTP}/assets/player.css" >"${WORK}/player.css"
if ! grep -Fq 'RTCPeerConnection' "${WORK}/player.js" || \
    ! grep -Fq 'recvonly' "${WORK}/player.js" || \
    ! grep -Fq 'xgc-app-shell' "${WORK}/player.js"; then
  log "embedded player script is incomplete"
  exit 1
fi
if ! grep -Fq '.xgc-topbar' "${WORK}/player.css" || \
    ! grep -Fq '.media-player-page' "${WORK}/player.css"; then
  log "embedded player stylesheet is incomplete"
  exit 1
fi

# No viewer exists in this contract test, so Edge deliberately leaves the
# source inactive.  The successful describe transaction plus live publisher
# and adapter processes are the readiness evidence; log formatting differs
# between rospy and rclpy and must not be part of the product contract.
sleep 2
if ! kill -0 "${ADAPTER_PID}" 2>/dev/null; then
  log "adapter died after edge start"
  cat "${WORK}/adapter.log"
  exit 1
fi
if ! kill -0 "${PUB_PID}" 2>/dev/null; then
  log "test publisher died after edge start"
  cat "${WORK}/publisher.log"
  exit 1
fi

log "OK: publisher + describe contract + media-edge healthz + embedded player assets"
cat "${WORK}/healthz.json"
exit 0
