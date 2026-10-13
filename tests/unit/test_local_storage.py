"""The file storage in a temporary directory."""

from pathlib import Path
from uuid import uuid4

import pytest

from chart_analyzer.application.ports.services import ChartImage
from chart_analyzer.domain.analysis import StoredImage
from chart_analyzer.infrastructure.storage.local import LocalImageStorage

PNG = ChartImage(data=b"\x89PNG fake bytes", content_type="image/png")


def file_names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


async def test_save_then_load_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "not" / "created" / "yet"
    storage = LocalImageStorage(root)
    analysis_id = uuid4()

    stored = await storage.save(analysis_id, PNG)

    assert stored == StoredImage(key=f"{analysis_id}.png", content_type="image/png")
    assert await storage.load(stored) == PNG
    assert file_names(root) == [f"{analysis_id}.png"]  # no temp file left


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [("image/png", "png"), ("image/jpeg", "jpg"), ("image/webp", "webp")],
)
async def test_the_extension_follows_the_content_type(
    tmp_path: Path, content_type: str, extension: str
) -> None:
    analysis_id = uuid4()

    stored = await LocalImageStorage(tmp_path).save(
        analysis_id, ChartImage(data=b"data", content_type=content_type)
    )

    assert stored.key == f"{analysis_id}.{extension}"
    assert (tmp_path / stored.key).read_bytes() == b"data"


async def test_an_unknown_content_type_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="content type"):
        await LocalImageStorage(tmp_path).save(
            uuid4(), ChartImage(data=b"data", content_type="text/html")
        )

    assert file_names(tmp_path) == []


async def test_saving_again_replaces_the_file(tmp_path: Path) -> None:
    storage = LocalImageStorage(tmp_path)
    analysis_id = uuid4()

    await storage.save(analysis_id, PNG)
    stored = await storage.save(analysis_id, ChartImage(data=b"second", content_type="image/png"))

    assert (await storage.load(stored)).data == b"second"


async def test_delete_removes_the_file(tmp_path: Path) -> None:
    storage = LocalImageStorage(tmp_path)
    stored = await storage.save(uuid4(), PNG)

    await storage.delete(stored)

    assert file_names(tmp_path) == []
    with pytest.raises(FileNotFoundError):
        await storage.load(stored)


async def test_delete_of_a_missing_file_is_silent(tmp_path: Path) -> None:
    storage = LocalImageStorage(tmp_path / "never-created")

    await storage.delete(StoredImage(key=f"{uuid4()}.png", content_type="image/png"))


@pytest.mark.parametrize(
    "key",
    ["../secret.png", "..\\secret.png", "nested/../../secret.png", "nested/inside.png", "", "."],
)
async def test_a_key_outside_the_directory_is_refused(tmp_path: Path, key: str) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    storage = LocalImageStorage(root)
    image = StoredImage(key=key, content_type="image/png")

    with pytest.raises(ValueError, match="outside"):
        await storage.load(image)
    with pytest.raises(ValueError, match="outside"):
        await storage.delete(image)

    assert secret.read_bytes() == b"secret"


async def test_an_absolute_key_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    storage = LocalImageStorage(tmp_path / "uploads")

    with pytest.raises(ValueError, match="outside"):
        await storage.load(StoredImage(key=str(secret), content_type="image/png"))
