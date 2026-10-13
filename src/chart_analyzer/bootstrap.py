"""Composition root: the only module that knows which adapter implements which port.

``build_api_services`` wires what the HTTP process needs, ``build_worker`` what the worker
process needs. Both are async context managers that close their connections on exit.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import httpx
from openai import AsyncOpenAI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from chart_analyzer.api.dependencies import ApiServices
from chart_analyzer.application.handle_task import RetryPolicy, TaskHandler
from chart_analyzer.application.ports.unit_of_work import UnitOfWorkFactory
from chart_analyzer.application.process_analysis import ProcessAnalysis
from chart_analyzer.application.queries import AnalysisQueries
from chart_analyzer.application.relay_outbox import RelayOutbox
from chart_analyzer.application.submit_analysis import SubmitAnalysis
from chart_analyzer.config import Settings
from chart_analyzer.domain.quota import QuotaPolicy
from chart_analyzer.infrastructure.ai.openai_chart_ai import OpenAIChartAI
from chart_analyzer.infrastructure.cache.redis_cache import RedisCache
from chart_analyzer.infrastructure.clock import SystemClock
from chart_analyzer.infrastructure.db.session import create_engine, create_session_factory
from chart_analyzer.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from chart_analyzer.infrastructure.events.redis_progress import RedisProgressStream
from chart_analyzer.infrastructure.market.cached import CachedMarketData
from chart_analyzer.infrastructure.market.finnhub import FinnhubMarketData
from chart_analyzer.infrastructure.messaging.connection import connect, open_channel
from chart_analyzer.infrastructure.messaging.consumer import TaskConsumer
from chart_analyzer.infrastructure.messaging.publisher import RabbitTaskPublisher
from chart_analyzer.infrastructure.messaging.topology import Topology
from chart_analyzer.infrastructure.storage.imaging import PillowImageInspector
from chart_analyzer.infrastructure.storage.local import LocalImageStorage


@dataclass(frozen=True, slots=True)
class Worker:
    """The two loops of the worker process."""

    consumer: TaskConsumer
    relay: RelayOutbox


def quota_policy(settings: Settings) -> QuotaPolicy:
    return QuotaPolicy(
        daily_limit=settings.quota_daily_tokens,
        reservation_tokens=settings.quota_reservation_tokens,
        max_active_analyses=settings.quota_max_active_analyses,
    )


def retry_policy(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        delays=settings.retry_delays_seconds, busy_delay=settings.retry_busy_delay_seconds
    )


def task_topology(settings: Settings) -> Topology:
    """Queues for every delay the retry policy can ask for."""
    policy = retry_policy(settings)
    return Topology(delays=(*policy.delays, policy.busy_delay))


def unit_of_work_factory(engine: AsyncEngine) -> UnitOfWorkFactory:
    session_factory = create_session_factory(engine)
    return lambda: SqlAlchemyUnitOfWork(session_factory)


def redis_client(settings: Settings) -> Redis:
    """Redis connection whose socket timeout outlasts the blocking read of the event stream.

    The client's default (5 s) is shorter than the keep-alive interval the stream blocks
    for, which would abort every idle read with a timeout.
    """
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.sse_keepalive_seconds + 5,
    )


def progress_stream(redis: Redis, settings: Settings) -> RedisProgressStream:
    return RedisProgressStream(
        redis,
        ttl_seconds=settings.progress_ttl_seconds,
        keepalive_seconds=settings.sse_keepalive_seconds,
    )


@asynccontextmanager
async def build_api_services(settings: Settings) -> AsyncIterator[ApiServices]:
    async with AsyncExitStack() as stack:
        engine = create_engine(settings.database_url)
        stack.push_async_callback(engine.dispose)
        redis = redis_client(settings)
        stack.push_async_callback(redis.aclose)

        uow_factory = unit_of_work_factory(engine)
        storage = LocalImageStorage(settings.upload_dir)
        policy = quota_policy(settings)
        clock = SystemClock()

        yield ApiServices(
            submit_analysis=SubmitAnalysis(
                uow_factory,
                storage,
                PillowImageInspector(
                    max_bytes=settings.max_image_bytes, max_side_px=settings.max_image_side_px
                ),
                policy,
                clock,
            ),
            queries=AnalysisQueries(uow_factory, storage, policy, clock),
            progress=progress_stream(redis, settings),
            max_image_bytes=settings.max_image_bytes,
        )


@asynccontextmanager
async def build_worker(settings: Settings) -> AsyncIterator[Worker]:
    async with AsyncExitStack() as stack:
        engine = create_engine(settings.database_url)
        stack.push_async_callback(engine.dispose)
        redis = redis_client(settings)
        stack.push_async_callback(redis.aclose)
        finnhub_http = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=settings.finnhub_base_url,
                headers={"X-Finnhub-Token": settings.finnhub_api_key.get_secret_value()},
                timeout=settings.finnhub_timeout_seconds,
            )
        )
        # No SDK-level retries: a failed call goes back to the queue, which owns backoff.
        openai_client = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url,
            timeout=settings.openai_timeout_seconds,
            max_retries=0,
        )
        stack.push_async_callback(openai_client.close)
        connection = await connect(settings.rabbitmq_url)
        stack.push_async_callback(connection.close)

        uow_factory = unit_of_work_factory(engine)
        clock = SystemClock()
        topology = task_topology(settings)

        market = CachedMarketData(
            FinnhubMarketData(
                finnhub_http,
                clock,
                news_lookback_days=settings.news_lookback_days,
                news_candidates=settings.news_candidates,
            ),
            RedisCache(redis),
            price_ttl=settings.cache_ttl_price_seconds,
            news_ttl=settings.cache_ttl_news_seconds,
            asset_ttl=settings.cache_ttl_asset_seconds,
            analyst_review_ttl=settings.cache_ttl_analyst_review_seconds,
        )
        process = ProcessAnalysis(
            uow_factory,
            LocalImageStorage(settings.upload_dir),
            OpenAIChartAI(
                openai_client,
                recognition_model=settings.openai_recognition_model,
                analysis_model=settings.openai_analysis_model,
                recognition_max_tokens=settings.openai_recognition_max_tokens,
                analysis_max_tokens=settings.openai_analysis_max_tokens,
                max_news=settings.max_news,
            ),
            market,
            progress_stream(redis, settings),
            clock,
            lease=timedelta(seconds=settings.analysis_lease_seconds),
            max_news=settings.max_news,
        )

        # The relay publishes on its own channel; the consumer opens another one for itself.
        relay_channel = await open_channel(connection)
        await topology.declare(relay_channel)

        yield Worker(
            consumer=TaskConsumer(
                connection,
                topology,
                TaskHandler(process, retry_policy(settings)),
                prefetch=settings.worker_prefetch,
            ),
            relay=RelayOutbox(
                uow_factory,
                RabbitTaskPublisher(relay_channel, topology),
                clock,
                batch_size=settings.outbox_batch_size,
            ),
        )
