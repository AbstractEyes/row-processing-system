"""Shrinking pictures before they are sent to a writer or a reviewer."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

JPEG_QUALITY = 90


class ImageError(ValueError):
    """The bytes are not a picture that can be read."""


def shrink_image(data: bytes, max_side: int | None) -> tuple[bytes, str, int]:
    """Return (picture bytes, file extension, longest side in pixels).

    A picture whose longest side is already at most `max_side` (or any picture when
    `max_side` is None) is returned unchanged if it is a JPEG or PNG, and as a JPEG
    otherwise. A larger picture is scaled down, keeping its proportions, and saved
    as a JPEG.
    """
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as error:  # Pillow raises several unrelated types for bad input
        raise ImageError(f"cannot read the picture: {error}") from None
    original_format = (image.format or "").upper()
    longest = max(image.size)
    if (max_side is None or longest <= max_side) and original_format in ("JPEG", "PNG"):
        return data, "jpg" if original_format == "JPEG" else "png", longest
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    if max_side is not None and max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), "jpg", max(image.size)


def load_picture(path: str | Path, max_side: int | None) -> tuple[bytes, str, int]:
    """Read a picture file and shrink it (see `shrink_image`)."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise ImageError(f"cannot read the picture file {str(path)!r}: {error}") from None
    return shrink_image(data, max_side)
