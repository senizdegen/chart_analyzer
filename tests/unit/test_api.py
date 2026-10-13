"""HTTP contract of the API, with every port replaced by an in-memory fake."""

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from chart_analyzer.api.app import create_app
from chart_analyzer.api.dependencies import ApiServices
from chart_analyzer.api.errors import parse_accept_language
from chart_analyzer.application.process_analysis import ProcessAnalysis
from chart_analyzer.application.queries import AnalysisQueries
from chart_analyzer.application.submit_analysis import SubmitAnalysis
from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode, Language
from chart_analyzer.domain.errors import InvalidImage
from chart_analyzer.domain.quota import QuotaPolicy
from tests.fakes import (
    NOW,
    FakeChartAI,
    FakeInspector,
    FakeMarketData,
    FixedClock,
    InMemoryDatabase,
    InMemoryImageStorage,
    InMemoryProgressStream,
    InMemoryUnitOfWork,
    make_analysis,
    make_recognition,
)

USER = {"x-user-id": "user-1"}
PNG = ("chart.png", b"png-bytes", "image/png")
MAX_IMAGE_BYTES = 1024


class PickyInspector(FakeInspector):
    def inspect(self, data: bytes):  # type: ignore[no-untyped-def]
        if data == b"not-an-image":
            raise InvalidImage(ErrorCode.INVALID_IMAGE)
        return super().inspect(data)


class System:
    """The API plus an in-process stand-in for the worker."""

    def __init__(self) -> None:
        self.db = InMemoryDatabase()
        self.storage = InMemoryImageStorage()
        self.progress = InMemoryProgressStream()
        self.ai = FakeChartAI()
        self.clock = FixedClock()
        policy = QuotaPolicy(daily_limit=500_000, reservation_tokens=20_000, max_active_analyses=3)

        def uow_factory() -> InMemoryUnitOfWork:
            return InMemoryUnitOfWork(self.db)

        self.app = create_app(
            ApiServices(
                submit_analysis=SubmitAnalysis(
                    uow_factory, self.storage, PickyInspector(), policy, self.clock
                ),
                queries=AnalysisQueries(uow_factory, self.storage, policy, self.clock),
                progress=self.progress,
                max_image_bytes=MAX_IMAGE_BYTES,
            )
        )
        self.worker = ProcessAnalysis(
            uow_factory,
            self.storage,
            self.ai,
            FakeMarketData(),
            self.progress,
            self.clock,
            lease=timedelta(seconds=180),
            max_news=3,
        )


@pytest.fixture
def system() -> System:
    return System()


@pytest.fixture
async def client(system: System) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=system.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def upload(client: httpx.AsyncClient, **kwargs: object) -> httpx.Response:
    options: dict[str, object] = {"headers": USER, "files": {"image": PNG}}
    return await client.post("/api/v1/analyses", **(options | kwargs))  # type: ignore[arg-type]


def parse_sse(body: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for frame in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in frame.splitlines() if not line.startswith(":")
        )
        if fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


class TestSubmit:
    async def test_accepts_a_chart_and_returns_at_once(self, client: httpx.AsyncClient) -> None:
        response = await upload(client, data={"language": "es"})

        body = response.json()
        assert response.status_code == 202
        assert body["status"] == "queued"
        assert body["language"] == "es"
        assert body["result"] is None and body["error"] is None
        assert body["events_url"] == f"/api/v1/analyses/{body['id']}/events"
        assert body["image_url"] == f"/api/v1/analyses/{body['id']}/image"

    async def test_language_defaults_to_english(self, client: httpx.AsyncClient) -> None:
        assert (await upload(client)).json()["language"] == "en"

    async def test_requires_the_user_header(self, client: httpx.AsyncClient) -> None:
        response = await upload(client, headers={})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "missing_user_id"

    async def test_rejects_an_unsupported_language(self, client: httpx.AsyncClient) -> None:
        response = await upload(client, data={"language": "ru"})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unsupported_language"

    async def test_rejects_a_missing_file(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/v1/analyses", headers=USER, data={"language": "en"})

        body = response.json()
        assert response.status_code == 422
        assert body["error"]["code"] == "validation_error"
        assert body["error"]["details"][0]["field"] == "body.image"

    async def test_rejects_a_file_that_is_not_an_image_in_the_users_language(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await upload(
            client,
            files={"image": ("x.png", b"not-an-image", "image/png")},
            data={"language": "de"},
        )

        assert response.status_code == 422
        assert response.json()["error"] == {
            "code": "invalid_image",
            "message": "Bitte lade eine gültige Bilddatei hoch (PNG, JPEG oder WebP).",
        }

    async def test_rejects_an_oversized_file(self, client: httpx.AsyncClient) -> None:
        big = ("big.png", b"x" * (MAX_IMAGE_BYTES + 1), "image/png")

        response = await upload(client, files={"image": big})

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "image_too_large"

    async def test_responses_carry_a_request_id(self, client: httpx.AsyncClient) -> None:
        response = await upload(client, headers=USER | {"x-request-id": "req-42"})

        assert response.headers["x-request-id"] == "req-42"


class TestIdempotency:
    async def test_replaying_a_key_returns_the_original_analysis(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        headers = USER | {"Idempotency-Key": "tap-1"}

        first = await upload(client, headers=headers)
        second = await upload(client, headers=headers)

        assert (first.status_code, second.status_code) == (202, 200)
        assert first.json()["id"] == second.json()["id"]
        assert len(system.db.rows) == 1


class TestQuota:
    async def test_reports_the_budget(self, client: httpx.AsyncClient, system: System) -> None:
        system.db.seed(
            make_analysis(
                uuid4(), status=AnalysisStatus.COMPLETED, tokens_used=100_000, completed_at=NOW
            )
        )
        await upload(client)  # one running analysis reserves 20k

        response = await client.get("/api/v1/quota", headers=USER)

        assert response.json() == {
            "daily_limit": 500_000,
            "used_tokens": 100_000,
            "reserved_tokens": 20_000,
            "remaining_tokens": 380_000,
            "resets_at": "2026-10-14T00:00:00Z",
        }

    async def test_rejects_uploads_once_the_quota_is_spent(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        system.db.seed(
            make_analysis(
                uuid4(), status=AnalysisStatus.COMPLETED, tokens_used=500_000, completed_at=NOW
            )
        )

        response = await upload(client, data={"language": "fr"})

        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
        error = response.json()["error"]
        assert error["code"] == "quota_exceeded"
        assert error["message"].startswith("Vous avez atteint votre limite quotidienne")

    async def test_limits_concurrent_analyses(self, client: httpx.AsyncClient) -> None:
        for _ in range(3):
            assert (await upload(client)).status_code == 202

        response = await upload(client)

        assert response.status_code == 429
        assert response.json()["error"]["code"] == "too_many_active_analyses"
        assert "retry-after" not in response.headers


class TestReadingAnalyses:
    async def test_completed_analysis_has_the_full_result(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        analysis_id = (await upload(client)).json()["id"]
        await system.worker.run(UUID(analysis_id))

        body = (await client.get(f"/api/v1/analyses/{analysis_id}", headers=USER)).json()

        assert body["status"] == "completed"
        assert body["tokens_used"] == 8_000
        assert body["asset"] == {"symbol": "AAPL", "name": "Apple Inc", "type": "stock"}
        result = body["result"]
        assert result["current_price"]["price"] == 340.97
        assert result["analyst_review"]["consensus"] == "buy"
        assert result["key_insights"]["trend"]["value"] == "bullish"
        assert result["trading_signals"]["position"] == "long"
        assert result["latest_news"][0]["sentiment"] == "positive"

    async def test_failed_analysis_explains_why(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        system.ai.recognition = make_recognition(is_chart=False, symbol=None, asset_type=None)
        analysis_id = (await upload(client, data={"language": "it"})).json()["id"]
        await system.worker.run(UUID(analysis_id))

        body = (await client.get(f"/api/v1/analyses/{analysis_id}", headers=USER)).json()

        assert body["status"] == "failed"
        assert body["result"] is None
        assert body["error"] == {
            "code": "unclear_chart",
            "message": "Carica un grafico di trading chiaro.",
        }

    async def test_unknown_analysis_is_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get(f"/api/v1/analyses/{uuid4()}", headers=USER)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "analysis_not_found"

    async def test_another_users_analysis_is_404(self, client: httpx.AsyncClient) -> None:
        analysis_id = (await upload(client)).json()["id"]

        for path in ("", "/events", "/image"):
            response = await client.get(
                f"/api/v1/analyses/{analysis_id}{path}", headers={"x-user-id": "intruder"}
            )
            assert response.status_code == 404, path

    async def test_malformed_id_is_a_validation_error(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/analyses/not-a-uuid", headers=USER)

        assert response.status_code == 422

    async def test_serves_the_uploaded_image(self, client: httpx.AsyncClient) -> None:
        analysis_id = (await upload(client)).json()["id"]

        response = await client.get(f"/api/v1/analyses/{analysis_id}/image", headers=USER)

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == b"png-bytes"


class TestHistory:
    async def test_lists_own_analyses_newest_first_without_results(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        older = make_analysis(uuid4(), created_at=NOW - timedelta(hours=2))
        newer = make_analysis(uuid4(), created_at=NOW - timedelta(hours=1))
        system.db.seed(older)
        system.db.seed(newer)
        system.db.seed(make_analysis(uuid4(), user_id="someone-else"))

        body = (await client.get("/api/v1/analyses", headers=USER)).json()

        assert [item["id"] for item in body["items"]] == [str(newer.id), str(older.id)]
        assert (body["total"], body["limit"], body["offset"]) == (2, 20, 0)
        assert "result" not in body["items"][0]

    async def test_paginates(self, client: httpx.AsyncClient, system: System) -> None:
        for hours in range(5):
            system.db.seed(make_analysis(uuid4(), created_at=NOW - timedelta(hours=hours)))

        body = (await client.get("/api/v1/analyses?limit=2&offset=4", headers=USER)).json()

        assert len(body["items"]) == 1 and body["total"] == 5

    @pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
    async def test_rejects_bad_paging(self, client: httpx.AsyncClient, query: str) -> None:
        assert (await client.get(f"/api/v1/analyses?{query}", headers=USER)).status_code == 422


class TestEventStream:
    async def test_streams_stages_then_the_result(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        analysis_id = (await upload(client)).json()["id"]
        # The client connects while the analysis is queued; the worker then runs.
        queued = system.db.analysis(UUID(analysis_id))
        await system.worker.run(UUID(analysis_id))
        system.db.seed(queued)  # the API sees it as still running when the stream opens

        response = await client.get(f"/api/v1/analyses/{analysis_id}/events", headers=USER)

        events = parse_sse(response.text)
        assert response.headers["content-type"].startswith("text/event-stream")
        assert [name for name, _ in events] == ["stage", "stage", "stage", "completed"]
        assert [data.get("stage") for _, data in events[:3]] == [
            "recognizing",
            "market_data",
            "analyzing",
        ]
        assert events[-1][1]["result"]["asset"]["symbol"] == "AAPL"  # type: ignore[index]

    async def test_resumes_after_the_last_seen_event(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        analysis_id = (await upload(client)).json()["id"]
        queued = system.db.analysis(UUID(analysis_id))
        await system.worker.run(UUID(analysis_id))
        system.db.seed(queued)

        response = await client.get(
            f"/api/v1/analyses/{analysis_id}/events", headers=USER | {"Last-Event-ID": "2"}
        )

        assert [name for name, _ in parse_sse(response.text)] == ["stage", "completed"]

    async def test_finished_analysis_answers_from_the_database(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        analysis_id = (await upload(client)).json()["id"]
        await system.worker.run(UUID(analysis_id))
        system.progress.events.clear()  # stored events expired

        response = await client.get(f"/api/v1/analyses/{analysis_id}/events", headers=USER)

        events = parse_sse(response.text)
        assert [name for name, _ in events] == ["completed"]
        assert events[0][1]["status"] == "completed"

    async def test_recovers_when_the_final_event_was_lost(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        analysis_id = UUID((await upload(client)).json()["id"])
        queued = system.db.analysis(analysis_id)
        await system.worker.run(analysis_id)
        finished = system.db.analysis(analysis_id)
        system.progress.events[analysis_id].pop()  # the final event never reached the stream

        # The stream opens on a running analysis, drains the stages, idles, then finds the
        # finished row in the database.
        system.db.seed(queued)
        lookups = 0
        original_get = AnalysisQueries.get

        async def get(self: AnalysisQueries, wanted: UUID, user_id: str):  # type: ignore[no-untyped-def]
            nonlocal lookups
            lookups += 1
            if lookups == 2:
                system.db.seed(finished)
            return await original_get(self, wanted, user_id)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(AnalysisQueries, "get", get)
            response = await client.get(f"/api/v1/analyses/{analysis_id}/events", headers=USER)

        assert [name for name, _ in parse_sse(response.text)] == [
            "stage",
            "stage",
            "stage",
            "completed",
        ]

    async def test_failed_analysis_ends_with_a_failed_event(
        self, client: httpx.AsyncClient, system: System
    ) -> None:
        system.ai.recognition = make_recognition(is_chart=False, symbol=None, asset_type=None)
        analysis_id = (await upload(client)).json()["id"]
        await system.worker.run(UUID(analysis_id))

        response = await client.get(f"/api/v1/analyses/{analysis_id}/events", headers=USER)

        name, data = parse_sse(response.text)[-1]
        assert name == "failed"
        assert data["error"]["code"] == "unclear_chart"  # type: ignore[index]


class TestErrorLanguage:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (None, Language.EN),
            ("", Language.EN),
            ("tr", Language.TR),
            ("pt-BR,pt;q=0.9,en;q=0.8", Language.PT),
            ("ru-RU, de;q=0.7", Language.DE),
            ("tl-PH", Language.FIL),
            ("fil", Language.FIL),
            ("ja, zh", Language.EN),
        ],
    )
    def test_accept_language_parsing(self, header: str | None, expected: Language) -> None:
        assert parse_accept_language(header) is expected

    async def test_errors_outside_an_upload_follow_accept_language(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(
            f"/api/v1/analyses/{uuid4()}", headers=USER | {"Accept-Language": "id-ID"}
        )

        assert response.json()["error"]["message"] == "Analisis tidak ditemukan."


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


async def test_openapi_documents_the_endpoints(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert set(paths) == {
        "/api/v1/analyses",
        "/api/v1/analyses/{analysis_id}",
        "/api/v1/analyses/{analysis_id}/events",
        "/api/v1/analyses/{analysis_id}/image",
        "/api/v1/quota",
        "/health",
    }
