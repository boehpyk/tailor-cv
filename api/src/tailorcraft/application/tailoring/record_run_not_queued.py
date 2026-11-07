"""The `RecordRunNotQueued` use case: G-14's second transaction, with the credit back (plan §0.4)."""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.tailoring.record_run_failure import RecordRunFailure
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
from tailorcraft.domain.tailoring.value_objects import TailoringRunId


@dataclass(frozen=True, slots=True)
class RecordRunNotQueuedCommand:
    tailoring_run_id: TailoringRunId


class RecordRunNotQueued:
    """A committed run the broker refused: `runs.find` -> `RecordRunFailure(NOT_QUEUED, now)`, so
    the run fails, its credit comes back and `runs_failed` counts, all in the handler's second
    transaction (F-10). Replaces the body of `_tailoring_handlers.record_not_queued`, so no router
    orchestrates billing.

    A run that is gone (`find` is `None`) is nothing to record: returns quietly. The handler commits;
    if that fails, the run stays `queued` and charged, and the sweep's lost-queued list recovers it
    (F-11, plan §0.5).
    """

    def __init__(
        self, runs: TailoringRunRepository, record_failure: RecordRunFailure, clock: Clock
    ) -> None:
        self._runs = runs
        self._record_failure = record_failure
        self._clock = clock

    async def __call__(self, cmd: RecordRunNotQueuedCommand) -> None:
        raise NotImplementedError
