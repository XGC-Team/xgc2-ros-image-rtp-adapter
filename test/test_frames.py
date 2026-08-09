import pytest

from ros_image_rtp_adapter.frames import (
    FrameValidationError,
    pack_raw_frame,
)


def test_raw_frame_strips_ros_row_padding_without_changing_pixels():
    # Two RGB pixels plus two padding bytes on each row.
    data = bytes([1, 2, 3, 4, 5, 6, 90, 91, 7, 8, 9, 10, 11, 12, 92, 93])
    frame = pack_raw_frame(
        data,
        width=2,
        height=2,
        step=8,
        encoding="rgb8",
        expected_width=2,
        expected_height=2,
        expected_encoding="rgb8",
    )

    assert frame.data == bytes(range(1, 13))
    jpeg = frame.to_jpeg()
    assert jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")


def test_raw_frame_rejects_implicit_format_or_dimension_conversion():
    with pytest.raises(FrameValidationError, match="encoding"):
        pack_raw_frame(
            bytes(16 * 16 * 3),
            width=16,
            height=16,
            step=48,
            encoding="rgb8",
            expected_width=16,
            expected_height=16,
            expected_encoding="bgr8",
        )
    with pytest.raises(FrameValidationError, match="dimensions"):
        pack_raw_frame(
            bytes(16 * 16 * 3),
            width=16,
            height=16,
            step=48,
            encoding="rgb8",
            expected_width=32,
            expected_height=16,
            expected_encoding="rgb8",
        )
