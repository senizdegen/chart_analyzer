"""Validation and normalization of uploaded charts with Pillow.

An upload is untrusted: the file name and the declared content type say nothing, so the
bytes are decoded completely and the format is taken from what the decoder found.

Normalizing bounds what happens later: a chart larger than the AI model needs is
downscaled (fewer image tokens), and re-encoding drops metadata such as EXIF and comments,
which may carry the user's location.
"""

import io

from PIL import Image, ImageOps

from chart_analyzer.application.ports.services import ChartImage
from chart_analyzer.domain.enums import ErrorCode
from chart_analyzer.domain.errors import InvalidImage

_ACCEPTED = ("PNG", "JPEG", "WEBP")
# Pillow reports a JPEG that carries extra frames (phone cameras) as MPO; the first frame
# is the picture, so it is handled as the JPEG it is.
_FORMATS = {"PNG": "PNG", "JPEG": "JPEG", "MPO": "JPEG", "WEBP": "WEBP"}
_CONTENT_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_SAVE_OPTIONS: dict[str, dict[str, int | bool]] = {
    "PNG": {"optimize": True},
    "JPEG": {"quality": 90, "optimize": True},
    "WEBP": {"quality": 90},
}
# A small file can declare a huge canvas (decompression bomb); refuse before decoding it.
_MAX_PIXELS = 40_000_000
_KEPT_INFO = ("transparency",)  # needed to draw palette images; everything else is metadata


class PillowImageInspector:
    def __init__(self, *, max_bytes: int, max_side_px: int) -> None:
        self._max_bytes = max_bytes
        self._max_side_px = max_side_px

    def inspect(self, data: bytes) -> ChartImage:
        if not data:
            raise InvalidImage(ErrorCode.INVALID_IMAGE, "the file is empty")
        if len(data) > self._max_bytes:
            raise InvalidImage(ErrorCode.IMAGE_TOO_LARGE, f"larger than {self._max_bytes} bytes")

        try:
            with Image.open(io.BytesIO(data), formats=_ACCEPTED) as source:
                if source.width * source.height > _MAX_PIXELS:
                    raise InvalidImage(ErrorCode.INVALID_IMAGE, "too many pixels")
                image_format = _FORMATS[source.format or ""]
                source.load()
                image = ImageOps.exif_transpose(source)
                if max(image.size) > self._max_side_px:
                    image.thumbnail(
                        (self._max_side_px, self._max_side_px), Image.Resampling.LANCZOS
                    )
                image.info = {name: image.info[name] for name in _KEPT_INFO if name in image.info}
                output = io.BytesIO()
                image.save(output, image_format, **_SAVE_OPTIONS[image_format])
        except InvalidImage:
            raise
        except Exception as exc:
            # Pillow reports broken input with many unrelated exception types
            # (OSError, SyntaxError, ValueError, struct.error, DecompressionBombError, ...).
            raise InvalidImage(ErrorCode.INVALID_IMAGE, type(exc).__name__) from exc

        return ChartImage(data=output.getvalue(), content_type=_CONTENT_TYPES[image_format])
