"""Opt-in isolated XRPC -> real subprocess encoder -> loopback RTP acceptance."""
from io import BytesIO
import email.parser
import email.policy
import json
import os
import socket
import time

from PIL import Image
import pytest
from xgc2_xrpc import Client, Runtime
from ros_image_rtp_adapter.runtime import ImageRtpAdapterRuntime
from ros_image_rtp_adapter.settings import AdapterSettings


@pytest.mark.skipif(os.environ.get("XGC2_TEST_NATIVE_MEDIA") != "1", reason="native encoder acceptance is opt-in")
@pytest.mark.parametrize("backend", ["ffmpeg", "gstreamer"])
@pytest.mark.parametrize("input_type", ["compressed", "raw"])
def test_real_xrpc_start_capture_rtp_and_reaped_stop(tmp_path, backend, input_type):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(0.05)
        settings = AdapterSettings.from_mapping({"source_id": "native", "control_socket": str(tmp_path / "n.sock"),
            "width": 320, "height": 180, "fps": 10.0, "bitrate": 800000,
            "encoder_backend": backend, "input_message_type": input_type, "raw_encoding": "rgb8",
            "rtp_port": receiver.getsockname()[1], "source_clock_domain": "device"})
        source = ImageRtpAdapterRuntime(settings)
        source.start()
        prefix = "/v1/media/sources/native/"
        try:
            with Runtime() as runtime, Client(settings.control_socket, runtime=runtime) as client:
                client.instance_id = client.json("/v1/describe", method="GET")["service_ref"]["instance_id"]
                start = client.json(prefix + "start", {})
                assert start["completion"] == "applied" and start["active"]
                process = source.encoder._proc
                assert process is not None and process.poll() is None
                packets, index, deadline = [], 0, time.monotonic() + 8.0
                while time.monotonic() < deadline and not packets:
                    image = Image.new("RGB", (320, 180), ((index * 11) % 255, 30, 180))
                    if input_type == "compressed":
                        output = BytesIO()
                        image.save(output, format="JPEG")
                        assert source.submit_compressed(output.getvalue(), "jpeg",
                            source_stamp_ns=index, frame_id="native_optical")
                    else:
                        assert source.submit_raw(image.tobytes(), width=320, height=180, step=960,
                            encoding="rgb8", source_stamp_ns=index, frame_id="native_optical")
                    try:
                        packets.append(receiver.recv(65535))
                    except socket.timeout:
                        pass
                    index += 1
                assert packets, source.encoder.diagnostic
                assert packets[0][0] >> 6 == 2 and packets[0][1] & 0x7f == 96
                response = client.call(prefix + "capture", {"snapshotId": "native-capture",
                                       "includeRgb": True, "requestKeyframe": False})
                parts = list(email.parser.BytesParser(policy=email.policy.default).parsebytes(
                    ("Content-Type: " + response.content_type + "\r\n\r\n").encode() + response.body).iter_parts())
                capture = json.loads(parts[0].get_payload(decode=True))
                assert capture["frameSequence"] == index
                assert capture["timestampNanoseconds"] == index - 1
                assert capture["timestampClockDomain"] == "device"
                assert capture["frameId"] == "native_optical"
                assert len(parts[2].get_payload(decode=True)) == 320 * 180 * 3
                stop = client.json(prefix + "stop", {})
                assert stop["completion"] == "applied" and not stop["active"]
                assert process.poll() is not None and not source.encoder.running
                status = client.json(prefix + "status", method="GET")
                assert not status["applied_active"] and status["native"]["rtp"]["pending"] == 0
        finally:
            source.stop()
