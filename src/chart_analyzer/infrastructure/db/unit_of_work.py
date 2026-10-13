"""Unit of Work over one ``AsyncSession``: one transaction, both repositories bound to it."""

from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chart_analyzer.application.ports.repositories import AnalysisRepository, OutboxRepository
from chart_analyzer.infrastructure.db.repositories import (
    SqlAlchemyAnalysisRepository,
    SqlAlchemyOutboxRepository,
)

# hashtextextended maps the key to the 64-bit integer an advisory lock needs. A collision
# between two users only makes them wait for each other; it cannot break the quota.
_LOCK_USER = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


class SqlAlchemyUnitOfWork:
    """Usage: ``async with SqlAlchemyUnitOfWork(session_factory) as uow: ...``.

    The transaction begins with the first statement. Leaving the block without ``commit()``
    rolls it back, and the connection goes back to the pool in every case.
    """

    analyses: AnalysisRepository
    outbox: OutboxRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        session = self._session_factory()
        self._session = session
        self.analyses = SqlAlchemyAnalysisRepository(session)
        self.outbox = SqlAlchemyOutboxRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._active_session()
        self._session = None
        try:
            await session.rollback()  # nothing to undo after a commit
        finally:
            await session.close()

    async def commit(self) -> None:
        await self._active_session().commit()

    async def lock_user(self, user_id: str) -> None:
        """Held until the transaction ends, so it needs no explicit release and cannot leak."""
        await self._active_session().execute(_LOCK_USER, {"key": f"quota:{user_id}"})

    def _active_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("the unit of work is used outside its 'async with' block")
        return self._session
