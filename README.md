# xgc2-ros-image-rtp-adapter

Parameterized **ROS 2** bridge from `sensor_msgs/msg/CompressedImage` (**JPEG**)
to the **[xgc2-media-edge](https://github.com/lxk36/xgc2-media-edge)** source
contract (**H264/RTP** on loopback + Unix control socket).

This is a **generic capability product**. It is not hard-coded to Odin, Thor,
NVIDIA, a topic, or a device path. Any publisher that emits JPEG
`CompressedImage` can be wired in by parameters. Encoding is an explicit
deployment choice:

| Backend | Default | Intended use |
|---------|---------|--------------|
| `ffmpeg` | yes (`libx264`) | Portable CPU path and integration baseline |
| `gstreamer` | opt-in | Configurable software or hardware element pipeline |

There is deliberately no `auto` backend. The product never guesses hardware;
the Session/deployment selects a reviewed profile and startup fails early when
its requested encoder or GStreamer element is unavailable.

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

Runtime includes FFmpeg plus standard GStreamer tools/plugins. Vendor plugins
remain supplied by the target platform image/runtime.

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

### Optional GStreamer / hardware profile

The GStreamer backend takes element factory names, caps, and JSON property maps
as parameters. The adapter passes a list of arguments directly to
`gst-launch-1.0`; it never invokes a shell or concatenates a vendor pipeline.

Jetson Thor has a separate example profile rather than a code branch:

```bash
profile="$(ros2 pkg prefix ros_image_rtp_adapter)/share/ros_image_rtp_adapter/config/jetson_thor_gstreamer.yaml"
ros2 run ros_image_rtp_adapter image_rtp_adapter --ros-args \
  --params-file "${profile}" \
  -p image_topic:=/camera/image_raw/compressed \
  -p source_id:=camera \
  -p rtp_port:=5004 \
  -p control_socket:=/tmp/xgc2-camera-rtp.sock
```

That profile requests `nvjpegdec` → `nvvidconv` → `nvv4l2h264enc`. Before
spawning the pipeline, the adapter verifies every requested factory with
`gst-inspect-1.0`. NVIDIA documents the accelerated GStreamer elements for
Jetson and directs Thor users away from NVIDIA FFmpeg hardware acceleration:
[accelerated GStreamer](https://docs.nvidia.com/jetson/archives/r38.4/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html),
[Jetson Linux 38.4 release notes](https://docs.nvidia.com/jetson/archives/r38.4/ReleaseNotes/Jetson_Linux_Release_Notes_r38.4.pdf).

For a different device, select `encoder_backend:=gstreamer` and substitute its
decoder/converter/encoder factories and property JSON. No rebuild is needed.

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
| `encoder_backend` | `ffmpeg` | Explicit `ffmpeg` or `gstreamer` selection |
| `encoder` | `libx264` | FFmpeg encoder name (compatibility parameter) |
| `ffmpeg_path` | `ffmpeg` | FFmpeg binary |
| `ffmpeg_encoder_args_json` | `[]` | Optional FFmpeg argument array; replaces codec defaults |
| `ffmpeg_video_filter` | generated scale/pad | Optional FFmpeg filter expression |
| `gstreamer_path` / `gstreamer_inspect_path` | standard command names | GStreamer binaries |
| `gstreamer_jpeg_parser` | `jpegparse` | JPEG parser factory |
| `gstreamer_jpeg_caps` | JPEG + stream rate | Input caps template |
| `gstreamer_jpeg_decoder` | `jpegdec` | JPEG decoder factory |
| `gstreamer_video_converter` | `videoconvert` | Scale/color converter factory |
| `gstreamer_video_scaler` | `videoscale` | Scaler factory (`identity` when converter also scales) |
| `gstreamer_raw_caps` | I420 output caps | Caps template; supports runtime markers |
| `gstreamer_h264_encoder` | `x264enc` | H264 encoder factory |
| `gstreamer_*_properties_json` | portable software defaults | Structured element properties |
| `drop_to_latest` | `true` | Keep only latest frame |
| `require_jpeg` | `true` | Reject non-JPEG compressed formats |

Supported runtime markers in JSON/caps are `@bitrate`, `@bitrate_kbps`,
`@fps`, `@gop`, `@width`, `@height`, and (caps only) `@fps_fraction`. They keep
profiles tied to the normal stream parameters without embedding device values
in Python code.

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
pytest test/test_control_socket.py test/test_encoder.py -q

# Full integration (requires go + ffmpeg + media-edge checkout)
export MEDIA_EDGE_DIR=/path/to/xgc2-media-edge
./scripts/integration_media_edge.sh

# Long-running browser preview with the portable GStreamer backend
ENCODER_BACKEND=gstreamer \
MEDIA_EDGE_DIR=/path/to/xgc2-media-edge \
./scripts/lab_video_preview.sh

# Same supervised preview command with the optional Thor deployment profile
ENCODER_BACKEND=gstreamer \
ENCODER_PARAMS_FILE="$PWD/config/jetson_thor_gstreamer.yaml" \
MEDIA_EDGE_DIR=/path/to/xgc2-media-edge \
./scripts/lab_video_preview.sh
```

## License

Apache-2.0
