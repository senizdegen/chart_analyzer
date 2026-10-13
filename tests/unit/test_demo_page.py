"""The demo page is served at the root and stays out of the documented API."""

from collections.abc import AsyncIterator

import httpx
import pytest

from chart_analyzer.api.app import create_app
from chart_analyzer.api.dependencies import ApiServices
from chart_analyzer.application.queries import AnalysisQueries
from chart_analyzer.application.submit_analysis import SubmitAnalysis
from chart_analyzer.domain.quota import QuotaPolicy
from tests.fakes import (
    FakeInspector,
    FixedClock,
    InMemoryDatabase,
    InMemoryImageStorage,
    InMemoryProgressStream,
    InMemoryUnitOfWork,
)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    db = InMemoryDatabase()
    storage = InMemoryImageStorage()
    clock = FixedClock()
    policy = QuotaPolicy(daily_limit=500_000, reservation_tokens=20_000, max_active_analyses=3)

    def uow_factory() -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(db)

    app = create_app(
        ApiServices(
            submit_analysis=SubmitAnalysis(uow_factory, storage, FakeInspector(), policy, clock),
            queries=AnalysisQueries(uow_factory, storage, policy, clock),
            progress=InMemoryProgressStream(),
            max_image_bytes=1024,
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_root_serves_the_demo_page(client: httpx.AsyncClient) -> None:
    response = await client.get("/")  # no x-user-id: the page itself is public

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Chart Analyzer demo</title>" in response.text


async def test_the_page_is_self_contained(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text

    # It has to work offline in the container: no CDN scripts, stylesheets or fonts.
    for external in ('src="http', "src='http", 'href="http', "@import", "url(http"):
        assert external not in page, external


async def test_the_page_is_not_part_of_the_documented_api(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert "/" not in paths
    assert "/api/v1/analyses" in paths
