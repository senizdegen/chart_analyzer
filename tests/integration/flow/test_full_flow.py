"""One analysis through the whole system.

    HTTP upload -> Postgres (analysis + outbox) -> outbox relay -> RabbitMQ -> consumer
    -> pipeline -> Postgres (result) + Redis (progress) -> HTTP result and event stream

Postgres, RabbitMQ and Redis are real. Only the two paid external services, the AI model
and the market data provider, are replaced with fakes.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from aio_pika.abc import AbstractRobustChannel, AbstractRobustConnection
from redis.asyncio import Redis
from tests.fakes import (
    FakeChartAI,
    FakeInspector,
    FakeMarketData,
    InMemoryImageStorage,
    make_recognition,
)

from chart_analyzer.api.app import create_app
from chart_analyzer.api.dependencies import ApiServices
from chart_analyzer.application.handle_task import RetryPolicy, TaskHandler
from chart_analyzer.application.ports.unit_of_work import UnitOfWork
from chart_analyzer.application.process_analysis import ProcessAnalysis
from chart_analyzer.application.queries import AnalysisQueries
from chart_analyzer.application.relay_outbox import RelayOutbox
from chart_analyzer.application.submit_analysis import SubmitAnalysis
from chart_analyzer.domain.enums import ErrorCode
from chart_analyzer.domain.errors import TransientFailure
from chart_analyzer.domain.quota import QuotaPolicy
from chart_analyzer.infrastructure.clock import SystemClock
from chart_analyzer.infrastructure.events.redis_progress import RedisProgressStream
from chart_analyzer.infrastructure.messaging.consumer import TaskConsumer
from chart_analyzer.infrastructure.messaging.publisher import RabbitTaskPublisher
from chart_analyzer.infrastructure.messaging.topology import Topology

pytestmark = pytest.mark.integration

USER = {"x-user-id": "flow-user"}
PNG = ("chart.png", b"png-bytes", "image/png")
TIMEOUT = 15.0


class System:
    def __init__(
        self,
        client: httpx.AsyncClient,
        relay: RelayOutbox,
        publisher: RabbitTaskPublisher,
        ai: FakeChartAI,
        market: FakeMarketData,
        dead_letters: Callable[[], Awaitable[int]],
    ) -> None:
        self.client = client
        self.relay = relay
        self.publisher = publisher
        self.ai = ai
        self.market = market
        self.dead_letters = dead_letters

    async def upload(self, language: str = "en", **headers: str) -> str:
        response = await self.client.post(
            "/api/v1/analyses",
            headers=USER | headers,
            files={"image": PNG},
            data={"language": language},
        )
        assert response.status_code in (200, 202), response.text
        return str(response.json()["id"])

    async def get(self, analysis_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/v1/analyses/{analysis_id}", headers=USER)
        body: dict[str, Any] = response.json()
        return body

    async def wait_finished(self, analysis_id: str) -> dict[str, Any]:
        async with asyncio.timeout(TIMEOUT):
            while True:
                body = await self.get(analysis_id)
                if body["status"] in ("completed", "failed"):
                    return body
                await asyncio.sleep(0.05)

    async def events(self, analysis_id: str) -> list[tuple[str, dict[str, Any]]]:
        response = await self.client.get(
            f"/api/v1/analyses/{analysis_id}/events", headers=USER, timeout=TIMEOUT
        )
        events = []
        for frame in response.text.strip().split("\n\n"):
            fields = dict(
                line.split(": ", 1) for line in frame.splitlines() if not line.startswith(":")
            )
            if fields:
                events.append((fields["event"], json.loads(fields["data"])))
        return events


@pytest.fixture
async def system(
    uow_factory: Callable[[], UnitOfWork],
    connection: AbstractRobustConnection,
    channel: AbstractRobustChannel,
    topology: Topology,
    redis: Redis,
) -> AsyncIterator[System]:
    clock = SystemClock()
    storage = InMemoryImageStorage()
    progress = RedisProgressStream(redis, ttl_seconds=60, keepalive_seconds=1)
    policy = QuotaPolicy(daily_limit=500_000, reservation_tokens=20_000, max_active_analyses=3)
    ai, market = FakeChartAI(), FakeMarketData()

    process = ProcessAnalysis(
        uow_factory,
        storage,
        ai,
        market,
        progress,
        clock,
        lease=timedelta(seconds=30),
        max_news=3,
    )
    # The test topology has a single 1-second delay queue: two retries, then the DLQ.
    consumer = TaskConsumer(
        connection,
        topology,
        TaskHandler(process, RetryPolicy(delays=(1, 1), busy_delay=1)),
        prefetch=4,
    )
    await consumer.start()

    app = create_app(
        ApiServices(
            submit_analysis=SubmitAnalysis(uow_factory, storage, FakeInspector(), policy, clock),
            queries=AnalysisQueries(uow_factory, storage, policy, clock),
            progress=progress,
            max_image_bytes=1024,
        )
    )
    publisher = RabbitTaskPublisher(channel, topology)
    relay = RelayOutbox(uow_factory, publisher, clock, batch_size=10)

    async def dead_letters() -> int:
        queue = await channel.get_queue(topology.dead_letter_queue, ensure=False)
        return (await queue.declare()).message_count or 0

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield System(client, relay, publisher, ai, market, dead_letters)
        finally:
            await consumer.stop()
            keys = [key async for key in redis.scan_iter("analysis:*:events")]
            if keys:
                await redis.delete(*keys)


async def test_uploaded_chart_becomes_a_completed_analysis(system: System) -> None:
    analysis_id = await system.upload(language="es")
    assert (await system.get(analysis_id))["status"] == "queued"  # nothing ran yet

    assert await system.relay.run_once() == 1
    analysis = await system.wait_finished(analysis_id)

    assert analysis["status"] == "completed"
    assert analysis["language"] == "es"
    assert analysis["tokens_used"] == 1_200 + 6_800
    assert analysis["asset"] == {"symbol": "AAPL", "name": "Apple Inc", "type": "stock"}
    assert analysis["result"]["current_price"]["price"] == 340.97
    assert analysis["result"]["trading_signals"]["position"] == "long"
    assert await system.relay.run_once() == 0  # the outbox row was marked sent

    quota = (await system.client.get("/api/v1/quota", headers=USER)).json()
    assert (quota["used_tokens"], quota["reserved_tokens"]) == (8_000, 0)

    history = (await system.client.get("/api/v1/analyses", headers=USER)).json()
    assert [item["id"] for item in history["items"]] == [analysis_id]


async def test_event_stream_reports_stages_and_the_result(system: System) -> None:
    analysis_id = await system.upload()
    stream = asyncio.create_task(system.events(analysis_id))  # the client connects first

    await system.relay.run_once()
    events = await stream

    assert [name for name, _ in events] == ["stage", "stage", "stage", "completed"]
    assert [data["stage"] for _, data in events[:3]] == [
        "recognizing",
        "market_data",
        "analyzing",
    ]
    assert events[-1][1]["id"] == analysis_id
    assert events[-1][1]["result"]["asset"]["symbol"] == "AAPL"


async def test_transient_failure_is_retried_from_the_checkpoint(system: System) -> None:
    system.market.error = TransientFailure(ErrorCode.MARKET_DATA_UNAVAILABLE)
    analysis_id = await system.upload()
    await system.relay.run_once()

    # First attempt fails after recognition; heal the provider before the delayed retry.
    async with asyncio.timeout(TIMEOUT):
        while system.market.calls == 0:
            await asyncio.sleep(0.02)
    system.market.error = None
    analysis = await system.wait_finished(analysis_id)

    assert analysis["status"] == "completed"
    assert system.ai.recognize_calls == 1  # the checkpoint was reused, not paid for again
    assert analysis["tokens_used"] == 8_000
    assert await system.dead_letters() == 0


async def test_exhausted_retries_fail_the_analysis_and_dead_letter_the_task(
    system: System,
) -> None:
    system.market.error = TransientFailure(ErrorCode.MARKET_DATA_UNAVAILABLE)
    analysis_id = await system.upload(language="de")
    await system.relay.run_once()

    analysis = await system.wait_finished(analysis_id)

    assert analysis["status"] == "failed"
    assert analysis["error"]["code"] == "market_data_unavailable"
    assert analysis["error"]["message"].startswith("Marktdaten sind vorübergehend")
    async with asyncio.timeout(TIMEOUT):
        while await system.dead_letters() == 0:
            await asyncio.sleep(0.05)
    assert await system.dead_letters() == 1
    quota = (await system.client.get("/api/v1/quota", headers=USER)).json()
    assert quota["used_tokens"] == 0  # failed analyses never count


async def test_image_that_is_not_a_chart_fails_without_retries(system: System) -> None:
    system.ai.recognition = make_recognition(is_chart=False, symbol=None, asset_type=None)
    analysis_id = await system.upload()
    await system.relay.run_once()

    analysis = await system.wait_finished(analysis_id)

    assert analysis["status"] == "failed"
    assert analysis["error"]["code"] == "unclear_chart"
    assert system.ai.recognize_calls == 1 and system.ai.analyze_calls == 0
    assert await system.dead_letters() == 0
    assert (await system.events(analysis_id))[-1][0] == "failed"


async def test_duplicate_task_does_not_run_the_analysis_twice(system: System) -> None:
    analysis_id = await system.upload()
    await system.relay.run_once()
    await system.wait_finished(analysis_id)

    # At-least-once delivery: the same task arrives again after the analysis finished.
    await system.publisher.publish(UUID(analysis_id))
    await asyncio.sleep(0.5)

    assert system.ai.analyze_calls == 1
    assert (await system.get(analysis_id))["tokens_used"] == 8_000


async def test_replayed_upload_creates_one_analysis_and_one_task(system: System) -> None:
    first = await system.upload(**{"Idempotency-Key": "tap-1"})
    second = await system.upload(**{"Idempotency-Key": "tap-1"})

    assert first == second
    assert await system.relay.run_once() == 1
