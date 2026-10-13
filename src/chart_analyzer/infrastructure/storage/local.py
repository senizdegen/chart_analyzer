"""Uploaded charts as files in one directory (a Docker volume shared by the API and the worker).

The API writes a file, the worker reads it later from another process, so a file appears
under its final name only when it is complete: it is written next to it and renamed.
File I/O is blocking, hence every operation runs in a thread.
"""

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

from chart_analyzer.application.ports.services import ChartImage
from chart_analyzer.domain.analysis import StoredImage

_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class LocalImageStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    async def save(self, analysis_id: UUID, image: ChartImage) -> StoredImage:
        extension = _EXTENSIONS.get(image.content_type)
        if extension is None:
            raise ValueError(f"unsupported content type {image.content_type!r}")
        stored = StoredImage(key=f"{analysis_id}.{extension}", content_type=image.content_type)
        await asyncio.to_thread(self._write, self._path(stored.key), image.data)
        return stored

    async def load(self, image: StoredImage) -> ChartImage:
        data = await asyncio.to_thread(self._path(image.key).read_bytes)
        return ChartImage(data=data, content_type=image.content_type)

    async def delete(self, image: StoredImage) -> None:
        await asyncio.to_thread(self._path(image.key).unlink, missing_ok=True)

    def _path(self, key: str) -> Path:
        """Keys come back from the database; one that leaves the directory is never followed."""
        path = (self._root / key).resolve()
        if path.parent != self._root:
            raise ValueError(f"image key {key!r} is outside the storage directory")
        return path

    def _write(self, path: Path, data: bytes) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
