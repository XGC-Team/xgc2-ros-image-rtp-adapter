#!/usr/bin/env python3
"""Focused process-level proof that the generic GStreamer backend emits RTP."""

from __future__ import annotations

import io
import socket
import time

from PIL import Image, ImageDraw

from ros_image_rtp_adapter.encoder import GStreamerJpegRtpEncoder


def make_jpeg(frame: int, width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (18, 24, 44))
    draw = ImageDraw.Draw(image)
    offset = (frame * 9) % max(1, width - 36)
    draw.rectangle((offset, 16, offset + 35, height - 16), fill=(0, 190, 255))
    draw.line((0, frame % height, width, (frame * 3) % height), fill=(255, 210, 0), width=4)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=80)
    return output.getvalue()


def main() -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.setblocking(False)
    rtp_port = int(receiver.getsockname()[1])
    encoder = GStreamerJpegRtpEncoder(
        gstreamer_path="gst-launch-1.0",
        gstreamer_inspect_path="gst-inspect-1.0",
        rtp_host="127.0.0.1",
        rtp_port=rtp_port,
        width=320,
        height=180,
        fps=10.0,
        bitrate=800_000,
    )

    packets = []
    try:
        encoder.start()
        deadline = time.monotonic() + 8.0
        frame = 0
        while time.monotonic() < deadline and len(packets) < 20:
            encoder.write_jpeg(make_jpeg(frame, 320, 180))
            frame += 1
            frame_deadline = time.monotonic() + 0.1
            while time.monotonic() < frame_deadline:
                try:
                    packets.append(receiver.recv(65535))
                except BlockingIOError:
                    time.sleep(0.005)
        if len(packets) < 3:
            process = encoder._proc
            return_code = process.poll() if process is not None else None
            raise RuntimeError(
                f"GStreamer produced only {len(packets)} RTP packets (rc={return_code}): "
                f"{encoder.diagnostic}"
            )
    finally:
        encoder.stop()
        receiver.close()

    for packet in packets:
        if len(packet) < 13:
            raise RuntimeError("short UDP payload is not RTP/H264")
        if packet[0] >> 6 != 2:
            raise RuntimeError("unexpected RTP version")
        if packet[1] & 0x7F != 96:
            raise RuntimeError("unexpected RTP payload type")
        nal_type = packet[12] & 0x1F
        if nal_type < 1 or nal_type > 31:
            raise RuntimeError("invalid H264 NAL payload")

    marker_packets = sum(1 for packet in packets if packet[1] & 0x80)
    if marker_packets < 1:
        raise RuntimeError("no RTP marker packet observed")
    print(
        "gstreamer RTP integration OK: "
        f"packets={len(packets)} marker_packets={marker_packets} port={rtp_port}"
    )


if __name__ == "__main__":
    main()
