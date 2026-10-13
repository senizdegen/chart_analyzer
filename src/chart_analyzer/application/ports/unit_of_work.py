"""Unit of Work: one database transaction with the repositories bound to it."""

from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self

from chart_analyzer.application.ports.repositories import AnalysisRepository, OutboxRepository


class UnitOfWork(Protocol):
    """Usage: ``async with uow_factory() as uow: ...; await uow.commit()``.

    Leaving the block without ``commit()`` rolls the transaction back.
    """

    analyses: AnalysisRepository
    outbox: OutboxRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def lock_user(self, user_id: str) -> None:
        """Serialize admissions of one user until the transaction ends.

        Must be the first statement of the transaction: everything read after it sees the
        rows committed by whoever held the lock before.
        """
        ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
