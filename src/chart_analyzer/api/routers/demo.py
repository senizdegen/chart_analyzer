"""A single static page for clicking through the API in a browser. Not part of the contract."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

_PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"


@router.get("/", include_in_schema=False)
async def demo_page() -> FileResponse:
    # no-cache: the page is tiny, and a reviewer should never see a stale copy after a rebuild.
    return FileResponse(
        _PAGE, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"}
    )
