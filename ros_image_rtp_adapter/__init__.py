"""ROS Image and CompressedImage to Media Edge H264/RTP adapter."""
import sys

if sys.version_info < (3, 10):
    raise RuntimeError("image RTP XRPC runtime requires Python >=3.10; Focal default Python 3.8 cannot run it")
