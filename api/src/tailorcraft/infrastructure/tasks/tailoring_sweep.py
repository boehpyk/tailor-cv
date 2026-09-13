"""The stale-run sweep task: a **thin entry point** for `AbandonStaleTailoringRuns` (G-25', ADR-0005).

Beat publishes it every minute (`tasks/app.py`'s `beat_schedule`). It resolves the dependencies, calls
the use case and translates the result into one log line, exactly as `run_tailoring` does for
`ExecuteTailoringRun`. Deciding which runs are stale, re-checking each one and recording it is
the use case's work. None of it happens here.

**This is the one recovery for a run whose worker was lost.** Redelivery is not
(`tasks/app.py`, at `task_acks_late`): a killed pool child, a hard time limit and a task that raised
all ack their message, so nothing comes back to finish the run. Without this task each of those
leaves a run `running` for ever, behind a UI that says "still working. Don't refresh."

**Nothing here handles a document.** The runs this task touches are `running`, so their document
columns are `NULL` by the table's own CHECK, and what it logs is two counts and a duration.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

import structlog

from tailorcraft.application.tailoring.abandon_stale_tailoring_runs import (
    AbandonStaleTailoringRunsResult,
)
from tailorcraft.infrastructure.tasks.app import ABANDON_STALE_TAILORING_RUNS_TASK_NAME, app
from tailorcraft.infrastructure.tasks.container import abandon_stale_runs_use_case

log = structlog.get_logger(__name__)

# G-25' names the success line. G-35 names only its field (`error_type`), so the failure line takes
# the same prefix and says what failed.
_EVENT_SWEPT: Final = "tailoring.stale_runs_swept"
_EVENT_SWEEP_FAILED: Final = "tailoring.stale_run_sweep_failed"


# `celery` is untyped, so the same narrow ignore as `run_tailoring`'s. The signature is fully
# annotated, so the body is still strictly checked.
@app.task(name=ABANDON_STALE_TAILORING_RUNS_TASK_NAME, bind=False, ignore_result=True)  # type: ignore[untyped-decorator]  # celery is untyped
def abandon_stale_tailoring_runs() -> None:
    """Record every stale `running` run as `failed` / `abandoned`, one bounded batch per tick.

    **Takes nothing and returns `None`, with `ignore_result=True`.** The use case decides what is
    stale from the clock, so there is no argument to pass. A return value would sit in Redis for
    `result_expires` with nobody to read it: beat publishes and never looks back.

    **No retry is declared, and none should be added**: no `autoretry_for`, no `retry_backoff`,
    no `max_retries`. **The next tick is the retry** (G-35). A sweep that failed because Postgres was
    down would be retried by Celery into the same outage, while beat publishes a fresh one a minute
    later regardless. That fresh one is idempotent by the aggregate's own rules: an abandoned run is
    no longer `running`, so it is never listed twice. A Celery retry would add a second schedule on
    top of the real one, and nothing to recover.

    **A failure escapes, after one line that carries only its type.** The exception is re-raised
    rather than swallowed so Celery marks the task failed and Sentry sees it. The line exists so a
    log search for the sweep finds its failures next to its successes. It logs the exception's type,
    never its message, which is the convention everywhere a database error can quote a row.
    """
    started_at = time.monotonic()
    try:
        # Fresh loop per tick, so the engine is built inside it (`tasks/container.py`).
        result = asyncio.run(_sweep())
    except Exception as exc:
        log.warning(
            _EVENT_SWEEP_FAILED,
            error_type=type(exc).__name__,
            duration_ms=_elapsed_ms(started_at),
        )
        raise

    # One line per tick, **zero counts included**. A sweep that found nothing is the normal case, and
    # logging it anyway makes the line a cheap proof of life. Beat has no other trace, and
    # `/health/ready` cannot see it (it pings workers, and beat is not one). A gap in these lines is
    # how a stopped beat shows up. `skipped_count` is a third field beyond the two G-25' names, on
    # purpose: the use case's result docstring calls a non-zero skip with no concurrent writer the
    # signature of adapter drift, and a count nobody logs is a signature nobody sees.
    log.info(
        _EVENT_SWEPT,
        swept_count=result.abandoned,
        skipped_count=result.skipped,
        duration_ms=_elapsed_ms(started_at),
    )


async def _sweep() -> AbandonStaleTailoringRunsResult:
    """Open the sweep's unit of work, run one batch, close the transaction.

    The load-bearing commits happen inside the use case, one per abandoned run, through
    `CommittingTailoringRunRepository`. This commit closes the read transaction the listing opened,
    which on a tick that found nothing is the only transaction there was. It sits inside the task's
    error boundary for the reason `run_tailoring`'s `_execute` gives.
    """
    async with abandon_stale_runs_use_case() as (sweep, session):
        result = await sweep()
        await session.commit()
        return result


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
