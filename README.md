# xgc2-ros-image-rtp-adapter

Parameterized **ROS 2** bridge from `sensor_msgs/msg/CompressedImage` (**JPEG**)
to the **[xgc2-media-edge](https://github.com/lxk36/xgc2-media-edge)** source
contract (**H264/RTP** on loopback + Unix control socket).

This is a **generic capability product**. It is **not** hard-coded to Odin or
any other brand. Any publisher that emits JPEG `CompressedImage` can be wired
in by changing the `image_topic` parameter.

```text
  [any camera driver]
        │  sensor_msgs/CompressedImage (jpeg)
        ▼
  ros_image_rtp_adapter   ← this package (topic fully parameterized)
        │  H264/RTP 127.0.0.1:<rtp_port>
        │  Unix control socket (describe / set-active / request-keyframe / snapshot)
        ▼
  xgc-media-edge
        │  WebRTC
        ▼
  browser / XGC2 camera-video panel
```

On the robot (e.g. Thor) you start **adapter + media-edge**. The ground station
only connects to the Edge pull URL (`edgeUrl` + `sourceId`).

## Branches

| Branch | Meaning |
|--------|---------|
| `jazzy` (default) | ROS 2 Jazzy / Ubuntu 24.04 (Noble) — primary for Thor |
| `humble` | ROS 2 Humble / Ubuntu 22.04 (Jammy) packaging track |
| `ros1` (future) | ROS 1 Noetic track — **not** in this tree yet |

## Install (APT, after release train)

```bash
# Jazzy / Noble
sudo apt update
sudo apt install ros-jazzy-xgc2-ros-image-rtp-adapter

# Humble / Jammy
sudo apt install ros-humble-xgc2-ros-image-rtp-adapter
```

Runtime depends on `ffmpeg` (soft H264 path) and standard ROS 2 Python packages.

## Run

```bash
source /opt/ros/jazzy/setup.bash

# Example: Odin compressed topic (name is only a parameter)
ros2 launch ros_image_rtp_adapter image_rtp_adapter.launch.py \
  image_topic:=/odin1/image/compressed \
  source_id:=odin1 \
  rtp_port:=5004 \
  control_socket:=/tmp/xgc2-odin-rtp.sock \
  width:=1280 height:=720 fps:=15.0

# Pair with media-edge (co-located)
xgc-media-edge \
  -control-address 0.0.0.0:18090 \
  -source-id odin1 \
  -rtp-listen-address 127.0.0.1:5004 \
  -source-control-socket /tmp/xgc2-odin-rtp.sock
```

Switch cameras without rebuilding:

```bash
image_topic:=/other_cam/image/compressed source_id:=other_cam
```

Message type must remain `sensor_msgs/msg/CompressedImage` with JPEG payload
when `require_jpeg:=true` (default).

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `image_topic` | `/camera/image_raw/compressed` | Input topic |
| `source_id` | `camera` | media-edge source id |
| `frame_id` | `camera_optical` | Optical frame for describe/snapshot |
| `rtp_host` | `127.0.0.1` | RTP destination (must stay loopback for Edge) |
| `rtp_port` | `5004` | RTP port (must match Edge) |
| `control_socket` | `/tmp/xgc2-image-rtp-adapter.sock` | Absolute Unix socket path |
| `width` / `height` / `fps` | 1280 / 720 / 15 | Output stream metadata + scale |
| `bitrate` | 2500000 | Target video bitrate |
| `encoder` | `libx264` | FFmpeg encoder name |
| `ffmpeg_path` | `ffmpeg` | Encoder binary |
| `drop_to_latest` | `true` | Keep only latest frame |
| `require_jpeg` | `true` | Reject non-JPEG compressed formats |

## CI matrix

GitHub Actions (`.github/workflows/ci.yml`):

| ROS | Ubuntu | Arch |
|-----|--------|------|
| jazzy | noble | amd64, arm64 |
| humble | jammy | amd64, arm64 |

Each cell: **build package → install deb smoke → unit tests → integration with
xgc2-media-edge** (clone public Edge, run test JPEG publisher + adapter + Edge,
assert control `describe` + `/healthz` + player page). Failures fail the job;
issues are not left to operators.

## Development

```bash
# In a ROS 2 workspace
colcon build --packages-select ros_image_rtp_adapter
source install/setup.bash
pytest test/test_control_socket.py -q

# Full integration (requires go + ffmpeg + media-edge checkout)
export MEDIA_EDGE_DIR=/path/to/xgc2-media-edge
./scripts/integration_media_edge.sh
```

## License

Apache-2.0
