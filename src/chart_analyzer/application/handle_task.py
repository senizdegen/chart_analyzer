"""Retry policy of the worker, kept free of any broker API so it can be unit-tested.

The consumer asks ``TaskHandler`` what to do with a delivered task and then just carries
out the returned ``Disposition`` (ack, schedule a retry, or dead-letter).
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import structlog

from chart_analyzer.application.process_analysis import ProcessAnalysis, ProcessOutcome
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
            if attempt >= self._policy.max_attempts:
                await self._process.give_up(analysis_id, failure.code)
                return Disposition(Action.DEAD_LETTER)
            delay = self._policy.delays[attempt - 1]
            log.info("task.retry_scheduled", attempt=attempt, delay_seconds=delay)
            return Disposition(Action.RETRY, delay_seconds=delay, next_attempt=attempt + 1)

        if outcome is ProcessOutcome.BUSY:
            # Another worker holds a fresh lease. If it died, the lease expires and a later
            # delivery takes over; this does not consume an attempt.
            return Disposition(
                Action.RETRY, delay_seconds=self._policy.busy_delay, next_attempt=attempt
            )
        return Disposition(Action.ACK)
