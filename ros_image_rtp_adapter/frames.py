"""ROS-version-neutral validation and packing for image frames."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Mapping, Tuple

from ros_image_rtp_adapter.encoder import packed_frame_bytes


_RAW_LAYOUTS: Mapping[str, Tuple[str, str, int]] = {
    "rgb8": ("RGB", "RGB", 3),
    "bgr8": ("RGB", "BGR", 3),
    "rgba8": ("RGBA", "RGBA", 4),
    "bgra8": ("RGBA", "BGRA", 4),
    "mono8": ("L", "L", 1),
}


class FrameValidationError(ValueError):
    """A ROS image does not match the explicit fixed source contract."""


@dataclass(frozen=True)
class RawFrame:
    data: bytes
    width: int
    height: int
    encoding: str

    def to_jpeg(self, quality: int = 90) -> bytes:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - package dependency gate
            raise RuntimeError("raw snapshots require python3-pil") from exc

        mode, decoder, _ = _RAW_LAYOUTS[self.encoding]
        image = Image.frombytes(
            mode,
            (self.width, self.height),
            self.data,
            "raw",
            decoder,
            0,
            1,
        )
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality)
        return output.getvalue()

    def to_rgb(self) -> bytes:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - package dependency gate
            raise RuntimeError("raw snapshots require python3-pil") from exc

        mode, decoder, _ = _RAW_LAYOUTS[self.encoding]
        image = Image.frombytes(
            mode,
            (self.width, self.height),
            self.data,
            "raw",
            decoder,
            0,
            1,
        )
        if image.mode != "RGB":
            image = image.convert("RGB")
        return image.tobytes()


def normalize_raw_encoding(value: str) -> str:
    encoding = value.strip().lower()
    if encoding not in _RAW_LAYOUTS:
        raise FrameValidationError(
            "raw_encoding must be one of: " + ", ".join(_RAW_LAYOUTS)
        )
    return encoding


def require_jpeg_bytes(data: bytes) -> bytes:
    if len(data) < 4 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise FrameValidationError("CompressedImage is not a complete JPEG frame")
    return data


def pack_raw_frame(
    data: bytes,
    *,
    width: int,
    height: int,
    step: int,
    encoding: str,
    expected_width: int,
    expected_height: int,
    expected_encoding: str,
) -> RawFrame:
    normalized = normalize_raw_encoding(encoding)
    expected = normalize_raw_encoding(expected_encoding)
    if normalized != expected:
        raise FrameValidationError(
            f"Image encoding {normalized!r} does not match raw_encoding {expected!r}"
        )
    if int(width) != int(expected_width) or int(height) != int(expected_height):
        raise FrameValidationError(
            f"Image dimensions {width}x{height} do not match configured "
            f"{expected_width}x{expected_height}"
        )
    if width < 1 or height < 1:
        raise FrameValidationError("Image dimensions must be positive")

    bytes_per_pixel = _RAW_LAYOUTS[normalized][2]
    row_bytes = int(width) * bytes_per_pixel
    if int(step) < row_bytes:
        raise FrameValidationError(
            f"Image step {step} is smaller than packed row size {row_bytes}"
        )
    required = int(step) * int(height)
    if len(data) < required:
        raise FrameValidationError(
            f"Image data has {len(data)} bytes, expected at least {required}"
        )

    if int(step) == row_bytes:
        packed = bytes(data[: packed_frame_bytes(normalized, width, height)])
    else:
        packed = b"".join(
            data[row * int(step) : row * int(step) + row_bytes]
            for row in range(int(height))
        )
    return RawFrame(
        data=packed,
        width=int(width),
        height=int(height),
        encoding=normalized,
    )
