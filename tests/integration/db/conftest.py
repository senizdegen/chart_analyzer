"""Fixtures of the Postgres tests: a migrated test database, emptied before every test.

The schema is built by the real Alembic migrations, so the tests also prove that the
migration and the adapters agree with each other.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from chart_analyzer.domain.analysis import Analysis, NewAnalysis, StoredImage
from chart_analyzer.domain.analysis_rules import assemble_result
from chart_analyzer.domain.enums import Language
from chart_analyzer.domain.models import AnalysisResult
from chart_analyzer.infrastructure.db.session import create_engine, create_session_factory
from chart_analyzer.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from tests.fakes import NOW, make_insights, make_snapshot

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://chart:chart@localhost:55432/chart_analyzer_test"
)
LEASE = timedelta(seconds=180)

UowFactory = Callable[[], SqlAlchemyUnitOfWork]


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    database = make_url(TEST_DATABASE_URL).database or ""
    if not database.endswith("_test"):
        # these tests drop and truncate tables
        raise RuntimeError(f"refusing to run against '{database}': not a test database")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config


@pytest.fixture(scope="session")
async def engine(alembic_config: Config) -> AsyncIterator[AsyncEngine]:
    # the pool must serve the concurrency tests: up to 30 transactions open at once
    engine = create_engine(TEST_DATABASE_URL, pool_size=30, max_overflow=10)
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS analyses, outbox, alembic_version"))
    # Alembic's env.py starts its own event loop, so it cannot run inside this one
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture(autouse=True)
async def clean_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE analyses, outbox RESTART IDENTITY"))


@pytest.fixture
def uow_factory(session_factory: async_sessionmaker[AsyncSession]) -> UowFactory:
    return lambda: SqlAlchemyUnitOfWork(session_factory)


# --- helpers: each one is its own committed transaction ---------------------------------------


def make_result() -> AnalysisResult:
    return assemble_result(Language.EN, make_snapshot(), make_insights(), max_news=3)


async def add_analysis(
    uow_factory: UowFactory,
    *,
    user_id: str = "user-1",
    idempotency_key: str | None = None,
    created_at: datetime = NOW,
) -> Analysis:
    analysis_id = uuid4()
    async with uow_factory() as uow:
        analysis = await uow.analyses.add(
            NewAnalysis(
                id=analysis_id,
                user_id=user_id,
                language=Language.EN,
                image=StoredImage(key=f"{analysis_id}.png", content_type="image/png"),
                idempotency_key=idempotency_key,
                created_at=created_at,
            )
        )
        await uow.commit()
    return analysis


async def add_processing(
    uow_factory: UowFactory, *, user_id: str = "user-1", at: datetime = NOW
) -> Analysis:
    analysis = await add_analysis(uow_factory, user_id=user_id, created_at=at)
    async with uow_factory() as uow:
        claimed = (await uow.analyses.claim(analysis.id, now=at, lease=LEASE)).analysis
        await uow.commit()
    assert claimed is not None
    return claimed


async def add_completed(
    uow_factory: UowFactory, *, user_id: str = "user-1", tokens: int, at: datetime = NOW
) -> Analysis:
    analysis = await add_processing(uow_factory, user_id=user_id, at=at)
    async with uow_factory() as uow:
        assert await uow.analyses.complete(
            analysis.id, make_result(), tokens=tokens, now=at, attempt=analysis.attempts
        )
        await uow.commit()
    return await load(uow_factory, analysis.id)


async def load(uow_factory: UowFactory, analysis_id: UUID) -> Analysis:
    async with uow_factory() as uow:
        analysis = await uow.analyses.get(analysis_id)
    assert analysis is not None
    return analysis


async def take_over(uow_factory: UowFactory, analysis: Analysis, *, at: datetime) -> Analysis:
    """Another worker claims a processing analysis whose lease has expired by ``at``."""
    async with uow_factory() as uow:
        claimed = (await uow.analyses.claim(analysis.id, now=at, lease=LEASE)).analysis
        await uow.commit()
    assert claimed is not None
    assert claimed.attempts == analysis.attempts + 1
    return claimed
