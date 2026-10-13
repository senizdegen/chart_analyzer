"""The Pillow inspector on images generated in memory."""

import io

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from chart_analyzer.domain.enums import ErrorCode
from chart_analyzer.domain.errors import InvalidImage
from chart_analyzer.infrastructure.storage.imaging import PillowImageInspector

MAX_BYTES = 200_000
MAX_SIDE = 100
ORIENTATION_TAG = 0x0112
MAKE_TAG = 0x010F


def encode(image: Image.Image, image_format: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, image_format, **options)
    return buffer.getvalue()


def picture(image_format: str, size: tuple[int, int] = (80, 40), **options: object) -> bytes:
    return encode(Image.new("RGB", size, "steelblue"), image_format, **options)


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


@pytest.fixture
def inspector() -> PillowImageInspector:
    return PillowImageInspector(max_bytes=MAX_BYTES, max_side_px=MAX_SIDE)


def assert_rejected(inspector: PillowImageInspector, data: bytes, code: ErrorCode) -> None:
    with pytest.raises(InvalidImage) as rejection:
        inspector.inspect(data)
    assert rejection.value.code is code


@pytest.mark.parametrize(
    ("image_format", "content_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_supported_formats_keep_their_format(
    inspector: PillowImageInspector, image_format: str, content_type: str
) -> None:
    result = inspector.inspect(picture(image_format))

    assert result.content_type == content_type
    normalized = decode(result.data)
    assert normalized.format == image_format
    assert normalized.size == (80, 40)


def test_the_format_is_decided_by_content_not_by_a_claim(inspector: PillowImageInspector) -> None:
    # The inspector is never told a file name or a content type: bytes are all it gets.
    assert inspector.inspect(picture("JPEG")).content_type == "image/jpeg"


def test_a_multi_frame_camera_jpeg_is_accepted_as_jpeg(inspector: PillowImageInspector) -> None:
    frames = [Image.new("RGB", (80, 40), color) for color in ("steelblue", "black")]
    data = encode(frames[0], "MPO", save_all=True, append_images=frames[1:])
    assert decode(data).format == "MPO"

    result = inspector.inspect(data)

    assert result.content_type == "image/jpeg"
    assert decode(result.data).format == "JPEG"


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
@pytest.mark.parametrize(
    ("size", "expected"),
    [((400, 200), (100, 50)), ((150, 600), (25, 100)), ((101, 101), (100, 100))],
)
def test_a_large_image_is_downscaled_keeping_its_aspect_ratio(
    inspector: PillowImageInspector,
    image_format: str,
    size: tuple[int, int],
    expected: tuple[int, int],
) -> None:
    result = inspector.inspect(picture(image_format, size))

    assert decode(result.data).size == expected


@pytest.mark.parametrize("size", [(100, 100), (100, 30), (3, 7)])
def test_an_image_within_the_limit_is_not_resized(
    inspector: PillowImageInspector, size: tuple[int, int]
) -> None:
    assert decode(inspector.inspect(picture("PNG", size)).data).size == size


def test_pixels_survive_normalization(inspector: PillowImageInspector) -> None:
    result = inspector.inspect(picture("PNG"))

    assert decode(result.data).convert("RGB").getpixel((10, 10)) == (70, 130, 180)


def test_palette_transparency_survives(inspector: PillowImageInspector) -> None:
    source = Image.new("P", (20, 20), 0)
    source.putpalette([255, 0, 0, 0, 255, 0])

    result = inspector.inspect(encode(source, "PNG", transparency=0))

    assert decode(result.data).convert("RGBA").getpixel((5, 5)) == (255, 0, 0, 0)


def test_too_many_bytes_is_image_too_large() -> None:
    data = picture("PNG")
    exact = PillowImageInspector(max_bytes=len(data), max_side_px=MAX_SIDE)
    one_less = PillowImageInspector(max_bytes=len(data) - 1, max_side_px=MAX_SIDE)

    assert exact.inspect(data).content_type == "image/png"
    assert_rejected(one_less, data, ErrorCode.IMAGE_TOO_LARGE)


def test_oversized_garbage_is_too_large_before_it_is_parsed() -> None:
    tiny = PillowImageInspector(max_bytes=10, max_side_px=MAX_SIDE)

    assert_rejected(tiny, b"x" * 11, ErrorCode.IMAGE_TOO_LARGE)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"just some text, definitely not a chart",
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
    ],
)
def test_empty_and_non_image_data_is_invalid(inspector: PillowImageInspector, data: bytes) -> None:
    assert_rejected(inspector, data, ErrorCode.INVALID_IMAGE)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_a_truncated_file_is_invalid(inspector: PillowImageInspector, image_format: str) -> None:
    noise = Image.effect_noise((90, 90), 60).convert("RGB")  # does not compress to nothing
    data = encode(noise, image_format)

    assert inspector.inspect(data).data
    assert_rejected(inspector, data[: len(data) // 2], ErrorCode.INVALID_IMAGE)


@pytest.mark.parametrize("image_format", ["GIF", "BMP", "TIFF", "ICO"])
def test_other_image_formats_are_invalid(
    inspector: PillowImageInspector, image_format: str
) -> None:
    assert_rejected(inspector, picture(image_format, (32, 32)), ErrorCode.INVALID_IMAGE)


def test_a_decompression_bomb_is_invalid() -> None:
    # 49 megapixels of one colour compress to a few kilobytes.
    bomb = encode(Image.new("1", (7000, 7000)), "PNG")
    inspector = PillowImageInspector(max_bytes=MAX_BYTES, max_side_px=MAX_SIDE)

    assert len(bomb) < MAX_BYTES
    assert_rejected(inspector, bomb, ErrorCode.INVALID_IMAGE)


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
def test_exif_orientation_is_applied(inspector: PillowImageInspector, image_format: str) -> None:
    # Stored as landscape with a red left half; orientation 6 means "rotate 90° clockwise
    # to display", which makes it portrait with the red half on top.
    source = Image.new("RGB", (80, 40), "blue")
    source.paste("red", (0, 0, 40, 40))
    exif = Image.Exif()
    exif[ORIENTATION_TAG] = 6

    normalized = decode(inspector.inspect(encode(source, image_format, exif=exif)).data)

    assert normalized.size == (40, 80)
    top, bottom = (
        normalized.convert("RGB").getpixel((20, 10)),
        normalized.convert("RGB").getpixel((20, 70)),
    )
    assert isinstance(top, tuple) and isinstance(bottom, tuple)
    assert top[0] > 200 and top[2] < 60
    assert bottom[2] > 200 and bottom[0] < 60
    assert ORIENTATION_TAG not in normalized.getexif()


@pytest.mark.parametrize(
    ("image_format", "options"),
    [
        ("JPEG", {"comment": b"shot at home-address"}),
        ("PNG", {}),
        ("WEBP", {"xmp": b"<x:xmpmeta>home-address</x:xmpmeta>"}),
    ],
)
def test_metadata_is_stripped(
    inspector: PillowImageInspector, image_format: str, options: dict[str, object]
) -> None:
    exif = Image.Exif()
    exif[MAKE_TAG] = "home-address"
    if image_format == "PNG":
        text = PngInfo()
        text.add_text("Comment", "home-address")
        options = {"pnginfo": text}
    data = encode(Image.new("RGB", (80, 40), "steelblue"), image_format, exif=exif, **options)
    assert data.count(b"home-address") == 2

    result = inspector.inspect(data)

    assert b"home-address" not in result.data
    normalized = decode(result.data)
    assert dict(normalized.getexif()) == {}
    assert not {"exif", "comment", "xmp", "Comment", "icc_profile"} & set(normalized.info)
