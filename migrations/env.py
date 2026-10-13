"""Alembic environment: runs the migrations through the application's async driver.

The URL comes from ``DATABASE_URL`` (``chart_analyzer.config``) unless the caller put
``sqlalchemy.url`` on the Alembic config, which is how the integration tests point the
migrations at their own database. Migrations are written by hand, so there is no
``target_metadata`` and no autogenerate.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from chart_analyzer.config import get_settings

config = context.config

if config.config_file_name is not None:
    # keep the loggers of a process that runs migrations programmatically (tests)
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of connecting (``alembic upgrade head --sql``)."""
    context.configure(url=_database_url(), literal_binds=True, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
