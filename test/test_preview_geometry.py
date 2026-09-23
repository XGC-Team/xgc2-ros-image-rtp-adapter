"""Preview downscale keeps AR projection aligned with the source CameraInfo.

Lichtblick builds the ImageMode plane from CameraInfo width/height and maps
the full decoded texture onto it edge to edge. A preview is therefore aligned
iff a source pixel centre ``c`` lands on preview pixel centre
``c' = (c + 0.5) * s - 0.5`` -- the same point the scaled CameraInfo
``K' = (f * s, (c + 0.5) * s - 0.5)`` projects to. These tests push real
markers, placed by projecting world points through a 4K pinhole camera,
through the adapter's actual FFmpeg preview command and measure where they
land in the decoded preview.
"""
import math
import random
import shutil
import subprocess
import threading

import pytest

from ros_image_rtp_adapter.encoder import FFmpegH264PreviewEncoder

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

MARKER = 16  # source pixels per marker side
SOURCE_4K = (3840, 2160)
# world_wide_4k30_110: 110 degree HFOV ideal pinhole at 3840 px.
FOCAL_4K = (SOURCE_4K[0] / 2) / math.tan(math.radians(110 / 2))


def scaled_intrinsics(fx, fy, cx, cy, sx, sy):
    """CameraInfo K for an edge-aligned resize by (sx, sy)."""

    return fx * sx, fy * sy, (cx + 0.5) * sx - 0.5, (cy + 0.5) * sy - 0.5


def rotation(roll, pitch, yaw):
    cr, sr, cp, sp, cy, sy = (math.cos(roll), math.sin(roll), math.cos(pitch),
                              math.sin(pitch), math.cos(yaw), math.sin(yaw))
    return [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr]]


def project(point, rot, translation, fx, fy, cx, cy):
    x, y, z = (sum(rot[r][c] * point[c] for c in range(3)) + translation[r] for r in range(3))
    return fx * x / z + cx, fy * y / z + cy


def world_markers(rot, translation, intrinsics, width, height, count=10, seed=3):
    """World points whose 4K projections are exact marker centres x0 + 7.5."""

    fx, fy, cx, cy = intrinsics
    rng = random.Random(seed)
    markers = []
    for _ in range(count):
        x0 = rng.randrange(64, width - 64 - MARKER)
        y0 = rng.randrange(64, height - 64 - MARKER)
        u, v = x0 + (MARKER - 1) / 2, y0 + (MARKER - 1) / 2
        depth = rng.uniform(2.0, 12.0)
        camera = ((u - cx) / fx * depth, (v - cy) / fy * depth, depth)
        # Invert camera = R * world + t.
        delta = [camera[r] - translation[r] for r in range(3)]
        world = [sum(rot[r][c] * delta[r] for r in range(3)) for c in range(3)]
        markers.append(((x0, y0), world))
    return markers


def render_jpeg(width, height, boxes):
    draw = ",".join(f"drawbox=x={x}:y={y}:w={MARKER}:h={MARKER}:color=white:t=fill" for x, y in boxes)
    return subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", f"color=c=black:s={width}x{height}", "-vf", f"{draw},format=gray",
        "-frames:v", "1", "-q:v", "2", "-f", "mjpeg", "pipe:1",
    ], stdout=subprocess.PIPE, check=True, timeout=30).stdout


def gray_frames(data, input_format, width, height):
    raw = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", input_format, "-i", "pipe:0",
        "-f", "rawvideo", "-pix_fmt", "gray", "-vsync", "0", "pipe:1",
    ], input=data, stdout=subprocess.PIPE, check=True, timeout=30).stdout
    size = width * height
    assert raw and len(raw) % size == 0
    return [raw[index:index + size] for index in range(0, len(raw), size)]


def centroid(pixels, width, u, v, radius):
    total = sx = sy = 0.0
    for y in range(int(v) - radius, int(v) + radius + 2):
        row = y * width
        for x in range(int(u) - radius, int(u) + radius + 2):
            value = pixels[row + x]
            if value > 16:  # ignore codec noise on the black background
                total += value
                sx += value * x
                sy += value * y
    assert total > 0, (u, v)
    return sx / total, sy / total


def encode_preview(jpeg, source, preview, frames=4):
    units, done = [], threading.Event()

    def receive(unit, _stamp):
        units.append(unit)
        if len(units) == frames:
            done.set()

    encoder = FFmpegH264PreviewEncoder(
        ffmpeg_path="ffmpeg", source_width=source[0], source_height=source[1],
        width=preview[0], height=preview[1], fps=30, bitrate=8_000_000,
        input_format="jpeg", encoder="libx264",
    )
    encoder.set_access_unit_callback(receive)
    encoder.start()
    try:
        for index in range(frames):
            encoder.write_frame(jpeg, 1_000_000_000 + index * 33_333_333)
        encoder._proc.stdin.close()
        assert done.wait(20), encoder.diagnostic
    finally:
        encoder.stop()
    return encoder, b"".join(units)


@pytest.mark.parametrize("source,preview,half", [
    (SOURCE_4K, (1920, 1080), True),   # production: MJPEG half-resolution IDCT
    ((2560, 1440), (1920, 1080), False),  # any other ratio: area average
], ids=["4k-to-1080p-lowres", "1440p-to-1080p-area"])
def test_preview_markers_land_where_the_scaled_camera_info_projects_them(source, preview, half):
    width, height = source
    scale = FOCAL_4K * width / SOURCE_4K[0]
    intrinsics = (scale, scale, (width - 1) / 2, (height - 1) / 2)
    rot, translation = rotation(0.05, -0.32, 0.4), (0.3, -1.1, 4.0)
    markers = world_markers(rot, translation, intrinsics, width, height)
    jpeg = render_jpeg(width, height, [box for box, _ in markers])

    # The measurement itself: source markers sit at their 4K projections.
    source_pixels = gray_frames(jpeg, "mjpeg", width, height)[0]
    for (x0, y0), world in markers:
        u, v = project(world, rot, translation, *intrinsics)
        measured = centroid(source_pixels, width, u, v, MARKER)
        assert math.dist(measured, (u, v)) < 0.05, (measured, (u, v))

    encoder, stream = encode_preview(jpeg, source, preview)
    assert encoder.half_resolution_decode is half
    decoded = gray_frames(stream, "h264", *preview)[-1]
    sx, sy = preview[0] / width, preview[1] / height
    preview_intrinsics = scaled_intrinsics(*intrinsics, sx, sy)
    errors, naive = [], []
    for (_box, world) in markers:
        expected = project(world, rot, translation, *preview_intrinsics)
        measured = centroid(decoded, preview[0], *expected, MARKER)
        errors.append(math.dist(measured, expected))
        u, v = project(world, rot, translation, *intrinsics)
        naive.append(math.dist(measured, (u * sx, v * sy)))
    # Within codec noise (<0.15 px at 8 Mbit/s) of the pixel-centre convention;
    # the naive c * s convention is off by (1 - s) / 2 per axis (0.35 px here).
    assert max(errors) < 0.15, errors
    assert min(naive) > 0.5 * math.hypot((1 - sx) / 2, (1 - sy) / 2), naive


def test_scaled_camera_info_is_the_edge_aligned_texture_mapping():
    """Lichtblick maps texture u in [0, 1] to CameraInfo pixels u * width."""

    rng = random.Random(11)
    fx = fy = FOCAL_4K
    cx, cy = 1918.7, 1081.2
    for _ in range(200):
        rot = rotation(rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-3, 3))
        translation = (rng.uniform(-2, 2), rng.uniform(-2, 2), rng.uniform(2, 6))
        world = [rng.uniform(-3, 3), rng.uniform(-3, 3), rng.uniform(-1, 1)]
        u, v = project(world, rot, translation, fx, fy, cx, cy)
        if not (0 <= u < 3840 and 0 <= v < 2160):
            continue
        # Texture coordinate of that point on the 4K CameraInfo plane ...
        tex_u, tex_v = (u + 0.5) / 3840, (v + 0.5) / 2160
        # ... is the same texel a 1080p texture shows at pixel centre ...
        texel = (tex_u * 1920 - 0.5, tex_v * 1080 - 0.5)
        # ... as projecting with the scaled 1080p CameraInfo.
        scaled = project(world, rot, translation, *scaled_intrinsics(fx, fy, cx, cy, 0.5, 0.5))
        assert math.dist(texel, scaled) < 1e-6
