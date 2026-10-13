"""Retry policy of the worker, kept free of any broker API so it can be unit-tested.

The consumer asks ``TaskHandler`` what to do with a delivered task and then just carries
out the returned ``Disposition`` (ack, schedule a retry, or dead-letter).
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import structlog

from chart_analyzer.application.process_analysis import ProcessAnalysis, ProcessOutcome
from chart_analyzer.domain.enums import ErrorCode
from chart_analyzer.domain.errors import TransientFailure

log = structlog.get_logger(__name__)


class Action(StrEnum):
    ACK = "ack"  # done, drop the message
    RETRY = "retry"  # ack and re-publish through a delay queue
    DEAD_LETTER = "dead_letter"  # reject without requeue: the broker routes it to the DLQ


@dataclass(frozen=True, slots=True)
class Disposition:
    action: Action
    delay_seconds: int = 0
    next_attempt: int = 1


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """``delays[i]`` is the pause before attempt ``i + 2``; attempts = len(delays) + 1."""

    delays: tuple[int, ...]
    busy_delay: int

    @property
    def max_attempts(self) -> int:
        return len(self.delays) + 1


class TaskHandler:
    def __init__(self, process: ProcessAnalysis, policy: RetryPolicy) -> None:
        self._process = process
        self._policy = policy

    async def handle(self, analysis_id: UUID, attempt: int) -> Disposition:
        try:
            outcome = await self._process.run(analysis_id)
        except TransientFailure as failure:
            return await self._retry_or_give_up(analysis_id, attempt, failure.code)
        except Exception:
            # Not a domain failure: the database or another dependency broke around the
            # pipeline (for example while claiming). Dead-lettering here would strand the
            # analysis in an active status, so it is retried like any transient failure.
            log.exception("task.infrastructure_error")
            return await self._retry_or_give_up(analysis_id, attempt, ErrorCode.INTERNAL_ERROR)

        if outcome is ProcessOutcome.BUSY:
            # Another worker holds a fresh lease. If it died, the lease expires and a later
            # delivery takes over; this does not consume an attempt.
            return Disposition(
                Action.RETRY, delay_seconds=self._policy.busy_delay, next_attempt=attempt
            )
        return Disposition(Action.ACK)

    async def _retry_or_give_up(
        self, analysis_id: UUID, attempt: int, code: ErrorCode
    ) -> Disposition:
        if attempt < self._policy.max_attempts:
            delay = self._policy.delays[attempt - 1]
            log.info("task.retry_scheduled", attempt=attempt, delay_seconds=delay)
            return Disposition(Action.RETRY, delay_seconds=delay, next_attempt=attempt + 1)
        try:
            await self._process.give_up(analysis_id, code)
        except Exception:
            # The failure could not be recorded; the stale-analysis sweep will do it.
            log.exception("task.give_up_failed")
        return Disposition(Action.DEAD_LETTER)
