"""The hand-written migration: reversible, and in step with the table definitions."""

import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from chart_analyzer.infrastructure.db.models import Base

pytestmark = pytest.mark.integration


async def existing_tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
    return set(rows.scalars())


async def test_downgrade_then_upgrade(engine: AsyncEngine, alembic_config: Config) -> None:
    assert await existing_tables(engine) == {"analyses", "outbox", "alembic_version"}

    # Alembic's env.py starts its own event loop, so it runs in a thread
    await asyncio.to_thread(command.downgrade, alembic_config, "base")
    assert await existing_tables(engine) == {"alembic_version"}

    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    assert await existing_tables(engine) == {"analyses", "outbox", "alembic_version"}

    # pooled connections may still hold statements prepared against the dropped tables
    await engine.dispose()


async def test_migrated_schema_matches_the_models(engine: AsyncEngine) -> None:
    def differences(connection: Connection) -> list[object]:
        return list(compare_metadata(MigrationContext.configure(connection), Base.metadata))

    async with engine.connect() as connection:
        assert await connection.run_sync(differences) == []
