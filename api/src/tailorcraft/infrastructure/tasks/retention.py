"""The guest-purge task: a **thin entry point** for `PurgeExpiredGuestSessions` (FR-6, ADR-0018).

Beat publishes it hourly — **only when `GUEST_PURGE_ENABLED` is true** (`tasks/app.py`'s
`_guest_purge_schedule`). It takes the lock, calls the use case, writes the heartbeat, logs one
line and translates the outcome. Deciding what is expired, collecting each session's file keys,
deleting rows and unlinking files is the use case's work, and none of it happens here (AC-28). The
module therefore imports no repository and no file store; the composition root binds both.

**This is the only unattended process in the product that deletes a stranger's data**, which is why
every choice here leans the same way:

- **No retry** (AC-29). No `autoretry_for`, no `retry_backoff`, no `max_retries`. **The next tick is
  the retry**, and it is safe because the run is idempotent by construction (AC-15): a deleted
  session is no longer expired-and-present, a second `DELETE` affects zero rows, and
  `FileStorePort.delete` is `missing_ok`. A Celery retry would add a second schedule on top of the
  real one — into the same outage, most likely — and recover nothing beat will not recover in an
  hour.
- **A held lock is not a failure** (R-8). A tick that collides with an operator's long `make purge`
  logs `outcome=skipped` and returns. That is normal operation, not a fault, and raising would put
  an error in Sentry for a job that correctly did nothing. The **CLI** translates the same outcome
  as exit 3, because an operator who typed a command is owed the news — the asymmetry is AC-20's
  point, not an inconsistency.
- **A failure escapes** (R-1, R-15). One line carrying the exception's *type*, then a re-raise, so
  Celery marks the task failed and Sentry sees it. **No heartbeat is written on that path**: a
  heartbeat says "the job last finished at", and a run that died did not finish. Recording it would
  make the one field an operator checks lie in the direction of reassurance.
- **The line is logged on every run, including the empty ones** (AC-21, R-14). A job that does
  nothing and logs nothing is indistinguishable from a job that never ran — and for this job
  "nothing to do" is the healthy outcome, so the empty line is the proof of life. Beat has no other
  trace: `/health/ready`'s Celery probe pings *workers*, and beat is not a worker (R-10).

**Nothing here can name a person.** The use case's channel is a `PurgeReport` of counts, a flag and
two tuples of failure records, and `ExpiringGuestSession` has nowhere to put a CV, a filename or a
path — so every line below is counts, a duration, a word, an opaque session id and an exception's
**class name**, by the shape of the types rather than by the author's care (Constitution §8, AC-38,
and AC-4's amendment). No failure line carries the exception's message and none carries `exc_info`:
a driver error quotes the row it refused, and here the row is a guest session.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

import structlog

from tailorcraft.domain.retention.value_objects import PurgeReport
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention.heartbeat import RedisPurgeHeartbeat
from tailorcraft.infrastructure.retention.lock import RedisPurgeLock
from tailorcraft.infrastructure.retention.log_events import (
    EVENT_FILE_UNLINK_FAILED,
    EVENT_PURGE_COMPLETED,
    EVENT_PURGE_FAILED,
    EVENT_PURGE_SKIPPED,
    EVENT_SESSION_PURGE_FAILED,
)
from tailorcraft.infrastructure.settings import get_settings
from tailorcraft.infrastructure.tasks.app import (
    PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME,
    PURGE_LOCK_TTL_SECONDS,
    app,
)
from tailorcraft.infrastructure.tasks.container import purge_expired_guest_sessions_use_case

log = structlog.get_logger(__name__)

# AC-21 names the completion line and its eight fields. The other four share its prefix so that one
# log search finds a job's skips and failures — the run's and the individual sessions' and files' —
# next to its successes, which for a job whose failure mode is silence is the whole point of
# searching.
#
# The names moved to `infrastructure/retention/log_events.py` at T23, when `purge-guests` became the
# second runner of this use case: the search only works if both entry points spell the line the same
# way, and two literals that must be identical are one rewording away from not being.
_EVENT_COMPLETED: Final = EVENT_PURGE_COMPLETED
_EVENT_SKIPPED: Final = EVENT_PURGE_SKIPPED
_EVENT_FAILED: Final = EVENT_PURGE_FAILED
_EVENT_SESSION_FAILED: Final = EVENT_SESSION_PURGE_FAILED
_EVENT_FILE_UNLINK_FAILED: Final = EVENT_FILE_UNLINK_FAILED


# `celery` is untyped, so the same narrow ignore as the sweeps'. The signature is fully annotated,
# so the body is still strictly checked.
@app.task(name=PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME, bind=False, ignore_result=True)  # type: ignore[untyped-decorator]  # celery is untyped
def purge_expired_guest_sessions() -> None:
    """Delete one bounded batch of expired guest data, oldest expiry first.

    **Takes nothing and returns `None`, with `ignore_result=True`.** The use case decides what is
    expired from the clock, so there is no argument to pass, and a return value would sit in Redis
    for `result_expires` with nobody to read it: beat publishes and never looks back. What an
    operator reads instead is `/health/ready`'s `jobs.guest_purge`, fed by the heartbeat this writes
    — and, above it, the **backlog**, which is the number ADR-0018 decision 4 says to trust.

    A fresh event loop per tick, because the engine must be built inside the loop that will use it
    (`tasks/container.py`).
    """
    asyncio.run(_purge())


async def _purge() -> None:
    """One tick: lock, run, heartbeat, line. Everything this module does, in the loop.

    The whole body is here rather than split across the sync wrapper because every step needs
    `await` — and because a reader checking AC-28's "thin" claim should be able to see the entry
    point's entire shape in one screen.
    """
    settings = get_settings()
    redis = create_redis(settings.redis_url)
    try:
        # The TTL is the derived constant, not a setting (ADR-0018 decision 7): it must outlast the
        # longest run that can still be holding it, and that bound is Celery's own hard time limit.
        # A constant derived from the limit it must exceed cannot be misconfigured, so this slice
        # adds no fourth startup refusal (AC-30).
        lock = RedisPurgeLock(redis, PURGE_LOCK_TTL_SECONDS)
        async with lock.hold() as hold:
            if not hold.may_run:
                # R-8 only. `may_run` is false for exactly one outcome — held by another run — and
                # **`UNAVAILABLE` runs anyway**: the lock fails open, because the cost of a skipped
                # purge is a broken privacy promise and the cost of an overlap is duplicated work on
                # an idempotent job (R-7, ADR-0018 decision 6). The lock adapter has already logged
                # `retention.lock_unavailable` in that case, so there is nothing to add here and
                # nothing to decide: `may_run` states the direction once, so no caller re-derives it.
                log.info(_EVENT_SKIPPED, reason="lock_held")
                return

            started_at = time.monotonic()
            try:
                report, overdue_after = await _run_purge()
            except Exception as exc:
                # R-1, R-2, R-15: the run failed. One line with the exception's **type** — never its
                # message and never `exc_info`, because a failed database write carries the row it
                # refused through the driver's message and the `raise ... from` chain, and that row
                # is a guest session. Then re-raise, so Celery records the failure and Sentry sees
                # it, and **no heartbeat is written**.
                #
                # The partial counts R-2 imagines are not available: the report is the use case's
                # only channel and an escaping exception carries none. That is the design, not a
                # gap — whatever had committed stays committed, and the *backlog* is what tells the
                # operator how much is still overdue. A count derived from a run that died would be
                # a second source of truth, and the weaker one.
                log.warning(
                    _EVENT_FAILED,
                    error_type=type(exc).__name__,
                    duration_ms=_elapsed_ms(started_at),
                )
                raise

            # The heartbeat records that the job **finished**, and it is written on every completed
            # run including the ones that deleted nothing (AC-21, R-14). `record` never raises: by
            # the time it runs, the irreversible work is done and succeeded, so failing the run over
            # a bookkeeping write would mark a successful purge as failed and hand Sentry an error
            # for work that completed (R-25). It logs its own `retention.heartbeat_unavailable`.
            #
            # `at` is a fresh instant from the same whole-second `SystemClock` the run used, not the
            # run's own `now`: this field answers "when did it last finish", and the run's instant is
            # when it *started*. `duration_ms` is the report's, so the line and the heartbeat quote
            # one number rather than two measurements of the same thing.
            await RedisPurgeHeartbeat(redis).record(
                at=SystemClock().now(),
                outcome="ok",
                sessions_deleted=report.sessions_deleted,
                files_unlinked=report.files_unlinked,
                duration_ms=report.duration_ms,
            )

            # R-3 and R-4: the detail behind `sessions_failed` and `files_failed`, one line per
            # element, **above** the summary that explains it — a reader scanning downward meets
            # the individual refusals and then the totals they add up to.
            #
            # A run with failures still **completed**: the heartbeat above is written, the line
            # below is logged, and this task returns normally. `sessions_failed > 0` is a batch
            # that carried on without those sessions (R-3), not a run that died — that path is the
            # `except` above, and it emits none of these, because an escaping exception carries no
            # report at all.
            #
            # `warning`, matching `_EVENT_FAILED`: something an operator should act on happened,
            # and the run's own outcome line is an `info` that cannot say so.
            #
            # Neither line can carry a message, an `exc_info`, a `FileRef` or a path — not by care
            # here but because `SessionPurgeFailure` and `FileUnlinkFailure` have no field to hold
            # one. `error_type` is `type(exc).__name__` at its only construction site.
            for session_failure in report.session_purge_failures:
                log.warning(
                    _EVENT_SESSION_FAILED,
                    guest_session_id=str(session_failure.session_id.value),
                    error_type=session_failure.error_type,
                )
            for file_failure in report.file_unlink_failures:
                log.warning(_EVENT_FILE_UNLINK_FAILED, error_type=file_failure.error_type)

            # AC-21's eight fields, every one of them a count, a duration or a flag.
            #
            # `dry_run` is read off the report rather than written as `False`, although this entry
            # point can never dry-run: the field says what the *report* is a record of, and a line
            # whose shape matches the CLI's is a line one log search can read across both.
            #
            # `overdue_after` is a fresh count taken after the run (see `OverdueBacklog`), not
            # `examined - sessions_deleted`. It is the number that cannot be faked by a job that is
            # not working, and a subtraction would be a derived copy that can disagree with the
            # resource — the 1.5 lesson about a second derivation, and ADR-0018 decision 4.
            # `overdue_after > 0` after a full tick is normal: the batch is bounded at 100 and the
            # next tick takes the rest.
            log.info(
                _EVENT_COMPLETED,
                sessions_deleted=report.sessions_deleted,
                sessions_failed=report.sessions_failed,
                files_unlinked=report.files_unlinked,
                files_failed=report.files_failed,
                examined=report.examined,
                overdue_after=overdue_after,
                dry_run=report.dry_run,
                duration_ms=report.duration_ms,
            )
    finally:
        # The client is built per tick, like the engine, and closed on every path. A pool left open
        # under a loop `asyncio.run` is about to close leaks a connection per hour, and the leak
        # surfaces as a Redis connection limit weeks later, blamed on whatever ran last.
        await redis.aclose()


async def _run_purge() -> tuple[PurgeReport, int]:
    """Open the purge's unit of work, run one batch, take the backlog, close the transaction.

    The load-bearing commits are inside the run — one per deleted session, through
    `CommittingExpiredGuestDataAdapter` — so that a failure part-way through leaves every session
    already deleted deleted, and the next tick lists only the rest. The commit here closes the read
    transaction the listing and the count opened, which on a tick that found nothing overdue is the
    only transaction there was. It sits inside the caller's error boundary for the reason the
    sweeps' does: a commit failure should be the task's recorded failure, not a surprise during
    teardown.

    The backlog is counted **before** that commit, on the same session and the same connection: a
    second engine for one `COUNT` would be a second connection per tick for a number that is one
    index range scan.
    """
    async with purge_expired_guest_sessions_use_case() as (purge, backlog, session):
        report = await purge()
        overdue_after = await backlog.count()
        await session.commit()
        return report, overdue_after


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
