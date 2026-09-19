"""The stale-job sweep task: a **thin entry point** for `AbandonStaleExportJobs` (X-29, ADR-0005).

Beat publishes it every minute (`tasks/app.py`'s `beat_schedule`). It resolves the dependencies,
calls the use case and translates the result into one log line, exactly as `abandon_stale_tailoring_
runs` does for its own sweep. Deciding which jobs are stale, re-checking each one and recording it is
the use case's work. None of it happens here.

**This is the one recovery for an export whose worker was lost.** Redelivery is not (`tasks/app.py`,
at `task_acks_late`): a killed pool child, a hard time limit (X-40) and a task that raised (X-32) all
ack their message, so nothing comes back to finish the render. Without this task each of those leaves
a job `rendering` for ever, behind a UI that says "still preparing your file", with a **Download**
button that never arrives and no way for the user to tell that from slow.

**Nothing here handles a document.** The jobs this task touches are `rendering`, so they carry a
byte size of `NULL` and a file key pointing at bytes that may not exist; what it logs is three counts
and a duration.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

import structlog

from tailorcraft.application.export.abandon_stale_export_jobs import AbandonStaleExportJobsResult
from tailorcraft.infrastructure.tasks.app import ABANDON_STALE_EXPORT_JOBS_TASK_NAME, app
from tailorcraft.infrastructure.tasks.container import abandon_stale_export_jobs_use_case

log = structlog.get_logger(__name__)

# X-29 names the success line's fields. X-38 names only `error_type`, so the failure line takes the
# same prefix and says what failed.
_EVENT_SWEPT: Final = "export.stale_jobs_swept"
_EVENT_SWEEP_FAILED: Final = "export.stale_job_sweep_failed"


# `celery` is untyped, so the same narrow ignore as `render_export`'s. The signature is fully
# annotated, so the body is still strictly checked.
@app.task(name=ABANDON_STALE_EXPORT_JOBS_TASK_NAME, bind=False, ignore_result=True)  # type: ignore[untyped-decorator]  # celery is untyped
def abandon_stale_export_jobs() -> None:
    """Record every stale `rendering` job as `failed` / `abandoned`, one bounded batch per tick.

    **Takes nothing and returns `None`, with `ignore_result=True`.** The use case decides what is
    stale from the clock, so there is no argument to pass. A return value would sit in Redis for
    `result_expires` with nobody to read it: beat publishes and never looks back.

    **No retry is declared, and none should be added**: no `autoretry_for`, no `retry_backoff`, no
    `max_retries`. **The next tick is the retry** (X-38). A sweep that failed because Postgres was
    down would be retried by Celery into the same outage, while beat publishes a fresh one a minute
    later regardless. That fresh one is idempotent by the aggregate's own rules: an abandoned job is
    no longer `rendering`, so it is never listed twice. A Celery retry would add a second schedule on
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

    # One line per tick, **including the empty ones, and that is the point of the line.** A sweep
    # that found nothing is the normal case; logging it anyway is what makes this a cheap proof of
    # life. Beat has no other trace, and `/health/ready` cannot see it — `control.ping` reaches
    # workers, and beat is not a worker (CLAUDE.md; slice 1.6's heartbeat is what finally covers it).
    # A job that does nothing and logs nothing is indistinguishable from a job that never ran, so a
    # gap in these lines is the only way a stopped beat shows up before somebody notices exports
    # stuck "preparing" for ever.
    #
    # `examined_count` is the third field beyond X-29's two, on purpose: it is the batch bound's own
    # readout, and `examined_count == 100` says a backlog is being worked through a batch at a time
    # and the next tick has more to do — exactly what an operator wants after an outage and cannot
    # infer from `swept_count` alone. The skips are `examined - swept - conflicts`, which is why the
    # result carries no `skipped` field. `conflicts` (X-39) counts jobs a worker or a redelivery
    # decided between the sweep's read and its write; whichever wrote first stands, so a conflict is
    # neither an error nor an abandonment — but a steady non-zero count says the sweep and the
    # workers are racing, which is worth a look at `EXPORT_STALE_AFTER_SECONDS` against the hard time
    # limit.
    log.info(
        _EVENT_SWEPT,
        swept_count=result.swept,
        conflicts=result.conflicts,
        examined_count=result.examined,
        duration_ms=_elapsed_ms(started_at),
    )


async def _sweep() -> AbandonStaleExportJobsResult:
    """Open the sweep's unit of work, run one batch, close the transaction.

    The load-bearing commits happen inside the use case, one per abandoned job, through
    `CommittingExportJobRepository`. This commit closes the read transaction the listing opened,
    which on a tick that found nothing is the only transaction there was. It sits inside the task's
    error boundary for the reason `render_export`'s `_execute` gives.
    """
    async with abandon_stale_export_jobs_use_case() as (sweep, session):
        result = await sweep()
        await session.commit()
        return result


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
