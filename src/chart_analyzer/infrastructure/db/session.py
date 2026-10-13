"""Engine and session factory. One engine per process, one session per unit of work."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, *, pool_size: int = 10, max_overflow: int = 10) -> AsyncEngine:
    """``pool_pre_ping`` replaces connections that Postgres closed while they sat in the pool."""
    return create_async_engine(
        database_url, pool_size=pool_size, max_overflow=max_overflow, pool_pre_ping=True
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions keep loaded values after commit: a unit of work returns them to its caller."""
    return async_sessionmaker(engine, expire_on_commit=False)
