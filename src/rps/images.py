"""Shrinking pictures before they are sent to a writer or a reviewer, and enlarging one for a reviewer's seat."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

JPEG_QUALITY = 90
ENLARGED_QUALITIES = (95, 90, 80, 70)  # an enlarged picture is saved at the first of these that keeps it under the size below
ENLARGED_MAX_BYTES = 3_500_000  # a provider takes a picture of at most 5 MB, and the base64 text of one is a third longer
ENLARGED_MAX_SIDE = 2576  # the longest side a current reader model takes as it is; a longer one it scales down itself
ORIENTATION_TAG = 0x0112  # the EXIF mark that says how a viewer should turn the picture


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


def enlarge_image(data: bytes, scale: float, max_side: int = ENLARGED_MAX_SIDE) -> tuple[bytes, str, tuple[int, int]]:
    """Return (picture bytes, file extension, (width, height)) of the picture enlarged `scale` times.

    For a reader that cannot make out small lettering at the size the writer was sent. The enlargement adds
    no information (the picture shows what it showed), only more pixels a letter. The proportions are kept
    and the filter is LANCZOS; the enlarged picture is saved as a JPEG of high quality. No side grows past
    `max_side`: the scale is cut to that. A picture that cannot grow (a scale of 1 or less, or a longest side
    already at `max_side` or beyond) is returned as it came, byte for byte, if it is a JPEG or PNG, and as a
    JPEG of its own size otherwise.

    The pixels are enlarged as they are stored: the picture is not turned, and an orientation mark in its
    EXIF data is carried over to the enlarged picture. So a reader of the enlarged picture is shown what a
    reader of the stored one is shown, whatever a reader does with that mark.

    The enlarged picture is held under `ENLARGED_MAX_BYTES`: one that is larger at the first quality is saved
    at the next lower one, and a picture still too large at the last is an `ImageError`, as is anything else
    that goes wrong while it is enlarged.
    """
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as error:  # Pillow raises several unrelated types for bad input
        raise ImageError(f"cannot read the picture: {error}") from None
    original_format = (image.format or "").upper()
    longest = max(image.size)
    factor = min(float(scale), max_side / longest) if longest else 1.0
    if factor <= 1.0 and original_format in ("JPEG", "PNG"):
        return data, "jpg" if original_format == "JPEG" else "png", image.size
    try:
        orientation = image.getexif().get(ORIENTATION_TAG)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if factor > 1.0:
            size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
            image = image.resize(size, Image.Resampling.LANCZOS)
        extra: dict[str, bytes] = {}
        if isinstance(orientation, int) and 2 <= orientation <= 8:
            exif = Image.Exif()
            exif[ORIENTATION_TAG] = orientation
            extra["exif"] = exif.tobytes()
        for quality in ENLARGED_QUALITIES:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True, **extra)
            if buffer.tell() <= ENLARGED_MAX_BYTES:
                return buffer.getvalue(), "jpg", image.size
    except Exception as error:  # whatever Pillow raises here, the caller is told one thing: this picture cannot be enlarged
        raise ImageError(f"cannot enlarge the picture: {error}") from None
    raise ImageError(f"the enlarged picture is larger than {ENLARGED_MAX_BYTES:,} bytes even at quality {ENLARGED_QUALITIES[-1]}")


def load_picture(path: str | Path, max_side: int | None) -> tuple[bytes, str, int]:
    """Read a picture file and shrink it (see `shrink_image`)."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise ImageError(f"cannot read the picture file {str(path)!r}: {error}") from None
    return shrink_image(data, max_side)
