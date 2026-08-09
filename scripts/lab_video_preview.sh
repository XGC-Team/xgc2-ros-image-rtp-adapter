#!/usr/bin/env bash
# Lab preview: fake JPEG ROS2 topic → image_rtp_adapter → media-edge.
# Intended to run inside a ROS 2 Jazzy container (or host with ROS 2 + go + ffmpeg).
# Keeps processes alive for browser preview until SIGINT/SIGTERM.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ROS_DISTRO="${ROS_DISTRO:-jazzy}"
MEDIA_EDGE_DIR="${MEDIA_EDGE_DIR:-}"
MEDIA_EDGE_BINARY="${MEDIA_EDGE_BINARY:-}"
SOURCE_ID="${SOURCE_ID:-lab_camera}"
RTP_PORT="${RTP_PORT:-15004}"
CONTROL_SOCKET="${CONTROL_SOCKET:-/tmp/xgc2-lab-image-rtp.sock}"
# Bind all interfaces so host browser can reach a container (use host net or -p).
EDGE_HTTP="${EDGE_HTTP:-0.0.0.0:18090}"
IMAGE_TOPIC="${IMAGE_TOPIC:-/lab/image/compressed}"
WIDTH="${WIDTH:-640}"
HEIGHT="${HEIGHT:-360}"
FPS="${FPS:-10}"
BITRATE="${BITRATE:-1500000}"
ENCODER_BACKEND="${ENCODER_BACKEND:-ffmpeg}"
ENCODER="${ENCODER:-libx264}"
ENCODER_PARAMS_FILE="${ENCODER_PARAMS_FILE:-}"
WORK="${WORK:-/tmp/xgc2-lab-video-preview}"

if [[ -z "${MEDIA_EDGE_DIR}" ]]; then
  if [[ -d "${ADAPTER_ROOT}/../../common/media-edge" ]]; then
    MEDIA_EDGE_DIR="$(cd "${ADAPTER_ROOT}/../../common/media-edge" && pwd)"
  elif [[ -d "${ADAPTER_ROOT}/../media-edge" ]]; then
    MEDIA_EDGE_DIR="$(cd "${ADAPTER_ROOT}/../media-edge" && pwd)"
  fi
fi
if [[ -z "${MEDIA_EDGE_BINARY}" && ( -z "${MEDIA_EDGE_DIR}" || ! -d "${MEDIA_EDGE_DIR}" ) ]]; then
  echo "MEDIA_EDGE_DIR not set / media-edge not found" >&2
  exit 1
fi
if [[ -n "${MEDIA_EDGE_BINARY}" && ! -x "${MEDIA_EDGE_BINARY}" ]]; then
  echo "MEDIA_EDGE_BINARY is not executable: ${MEDIA_EDGE_BINARY}" >&2
  exit 1
fi
case "${ENCODER_BACKEND}" in
  ffmpeg|gstreamer) ;;
  *) echo "ENCODER_BACKEND must be ffmpeg or gstreamer" >&2; exit 1 ;;
esac
if [[ -n "${ENCODER_PARAMS_FILE}" && ! -f "${ENCODER_PARAMS_FILE}" ]]; then
  echo "ENCODER_PARAMS_FILE does not exist: ${ENCODER_PARAMS_FILE}" >&2
  exit 1
fi

log() { printf '[lab-video] %s\n' "$*"; }

mkdir -p "${WORK}"
rm -f "${CONTROL_SOCKET}"

cleanup() {
  set +e
  log "stopping..."
  [[ -n "${PUB_PID:-}" ]] && kill "${PUB_PID}" 2>/dev/null
  [[ -n "${ADAPTER_PID:-}" ]] && kill "${ADAPTER_PID}" 2>/dev/null
  [[ -n "${EDGE_PID:-}" ]] && kill "${EDGE_PID}" 2>/dev/null
  wait 2>/dev/null || true
  rm -f "${CONTROL_SOCKET}"
}
trap cleanup EXIT INT TERM

for bin in colcon curl python3 rsync; do
  command -v "${bin}" >/dev/null || {
    echo "missing required binary: ${bin}" >&2
    exit 1
  }
done
# go is only required when the media-edge binary is not prebuilt into WORK.

set +u
# shellcheck disable=SC1091
source "/opt/ros/${ROS_DISTRO}/setup.bash"
set -u

log "sync + colcon build ros_image_rtp_adapter → ${WORK}"
mkdir -p "${WORK}/src"
# Copy sources into a writable build tree. Re-sync every run so the documented
# preview command can never silently execute a stale adapter build.
rsync -a --delete \
  --exclude .work --exclude debs --exclude .git --exclude install --exclude build --exclude log \
  "${ADAPTER_ROOT}/" "${WORK}/src/ros_image_rtp_adapter/"
(
  cd "${WORK}"
  set +u
  # shellcheck disable=SC1091
  source "/opt/ros/${ROS_DISTRO}/setup.bash"
  set -u
  colcon build --packages-select ros_image_rtp_adapter --symlink-install
)

set +u
# shellcheck disable=SC1091
source "${WORK}/install/setup.bash"
set -u

if [[ -n "${MEDIA_EDGE_BINARY}" ]]; then
  cp "${MEDIA_EDGE_BINARY}" "${WORK}/xgc-media-edge"
elif [[ ! -x "${WORK}/xgc-media-edge" ]]; then
  command -v go >/dev/null || {
    echo "missing go and no prebuilt ${WORK}/xgc-media-edge" >&2
    exit 1
  }
  log "building media-edge"
  (
    cd "${MEDIA_EDGE_DIR}"
    go build -o "${WORK}/xgc-media-edge" ./cmd/xgc-media-edge
  )
fi

log "publisher ${IMAGE_TOPIC} ${WIDTH}x${HEIGHT}@${FPS}"
ros2 run ros_image_rtp_adapter publish_test_jpeg \
  --topic "${IMAGE_TOPIC}" --width "${WIDTH}" --height "${HEIGHT}" --fps "${FPS}" \
  >"${WORK}/publisher.log" 2>&1 &
PUB_PID=$!

log "adapter source_id=${SOURCE_ID} backend=${ENCODER_BACKEND} rtp=127.0.0.1:${RTP_PORT}"
ENCODER_ARGS=(
  -p encoder_backend:="${ENCODER_BACKEND}"
)
if [[ "${ENCODER_BACKEND}" == "ffmpeg" ]]; then
  ENCODER_ARGS+=(
    -p encoder:="${ENCODER}"
    -p ffmpeg_path:=ffmpeg
  )
fi
PARAMS_FILE_ARGS=()
if [[ -n "${ENCODER_PARAMS_FILE}" ]]; then
  PARAMS_FILE_ARGS+=(--params-file "${ENCODER_PARAMS_FILE}")
fi
ros2 run ros_image_rtp_adapter image_rtp_adapter --ros-args \
  "${PARAMS_FILE_ARGS[@]}" \
  -p image_topic:="${IMAGE_TOPIC}" \
  -p source_id:="${SOURCE_ID}" \
  -p frame_id:=lab_optical \
  -p rtp_host:=127.0.0.1 \
  -p rtp_port:="${RTP_PORT}" \
  -p control_socket:="${CONTROL_SOCKET}" \
  -p width:="${WIDTH}" \
  -p height:="${HEIGHT}" \
  -p fps:="${FPS}.0" \
  -p bitrate:="${BITRATE}" \
  "${ENCODER_ARGS[@]}" \
  -p drop_to_latest:=true \
  -p require_jpeg:=true \
  >"${WORK}/adapter.log" 2>&1 &
ADAPTER_PID=$!

log "wait control socket describe"
python3 "${SCRIPT_DIR}/wait_describe.py" \
  --socket "${CONTROL_SOCKET}" \
  --source-id "${SOURCE_ID}" \
  --rtp-port "${RTP_PORT}" \
  --timeout 90

log "media-edge http://${EDGE_HTTP}/  source=${SOURCE_ID}"
# public-ip helps host-browser WebRTC ICE when Edge is in a container/host net.
PUBLIC_IP_ARGS=()
if [[ -n "${PUBLIC_IP:-}" ]]; then
  PUBLIC_IP_ARGS+=(-public-ip "${PUBLIC_IP}")
fi
"${WORK}/xgc-media-edge" \
  -control-address "${EDGE_HTTP}" \
  -source-id "${SOURCE_ID}" \
  -rtp-listen-address "127.0.0.1:${RTP_PORT}" \
  -source-control-socket "${CONTROL_SOCKET}" \
  "${PUBLIC_IP_ARGS[@]}" \
  >"${WORK}/edge.log" 2>&1 &
EDGE_PID=$!

deadline=$((SECONDS + 60))
while (( SECONDS < deadline )); do
  if curl -fsS "http://127.0.0.1:${EDGE_HTTP##*:}/healthz" >/dev/null 2>&1 \
    || curl -fsS "http://${EDGE_HTTP}/healthz" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "${EDGE_PID}" 2>/dev/null; then
    log "media-edge died"
    cat "${WORK}/edge.log" || true
    exit 1
  fi
  sleep 0.3
done

PORT="${EDGE_HTTP##*:}"
log "READY ${ENCODER_BACKEND} lab preview"
log "  open browser:  http://127.0.0.1:${PORT}/"
log "  healthz:       http://127.0.0.1:${PORT}/healthz"
log "  source_id:     ${SOURCE_ID}"
log "  logs:          ${WORK}/*.log"
log "  Ctrl+C to stop"

# Light resource sample every 5s while running.
while kill -0 "${EDGE_PID}" 2>/dev/null \
  && kill -0 "${ADAPTER_PID}" 2>/dev/null \
  && kill -0 "${PUB_PID}" 2>/dev/null; do
  if command -v ps >/dev/null; then
    cpu="$(ps -o pid=,pcpu=,comm= -p "${PUB_PID},${ADAPTER_PID},${EDGE_PID}" 2>/dev/null | tr '\n' ' | ' || true)"
    log "cpu sample: ${cpu}"
  fi
  # Sample either backend child if present.
  pgrep -a 'ffmpeg|gst-launch-1.0' 2>/dev/null | head -3 | while read -r line; do
    log "encoder: ${line}"
  done || true
  sleep 5
done

log "a child process exited; dumping tails"
tail -n 40 "${WORK}/publisher.log" "${WORK}/adapter.log" "${WORK}/edge.log" || true
exit 1
