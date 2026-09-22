"""Readiness probes.

`/health/ready` answers one question: *can this process do its job right now?* Answering it honestly
means probing the things that can be down, and this stack has three.

The third one is the point. Postgres and Redis are the obvious two, and probing only those was
enough to let the previous project run for an entire session with a crash-looping background worker
behind a green dashboard. **A stopped worker looks exactly like a healthy system** if nothing asks
it a question: the API answers, the database answers, and every export queues silently forever
(ADR-0005). So Celery is probed too.

Slice 1.6 adds a *second kind* of thing this module reports, and it is deliberately not a
dependency: `probe_guest_purge` answers "is the guest purge keeping up?", which is a fact about
background hygiene rather than about this process's ability to serve a request. It returns a
`JobStatus`, never a `ProbeResult` — see that class for why the type is the safety mechanism
(ADR-0019 decision 2).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.infrastructure.persistence.database import create_session_factory
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention.data_access import OverdueBacklog
from tailorcraft.infrastructure.retention.heartbeat import (
    PurgeHeartbeat,
    PurgeOutcome,
    RedisPurgeHeartbeat,
)
from tailorcraft.infrastructure.settings import Settings

log = structlog.get_logger(__name__)

PROBE_TIMEOUT_SECONDS = 2.0

_GUEST_PURGE_FAILED_EVENT: Final = "probe.guest_purge.failed"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The outcome of probing one dependency."""

    name: str
    healthy: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class JobStatus:
    """What `/health/ready` publishes about one **scheduled job** (ADR-0019 decision 3).

    **This is not a `ProbeResult`, and the separate type is the whole safety mechanism** — not a
    taste in naming (ADR-0019 decision 2). `routers/health.py` decides the status code with

        all(r.healthy for r in results)

    and that line already exists. A `JobStatus` has no `healthy` attribute and is not in `results`,
    so a careless edit that swept this into that list would not merely be *discouraged*: it would
    not type-check and it would not run. The alternative considered and rejected was a `ProbeResult`
    with a `blocking: bool` flag, which keeps the wrong type in the right list and moves the safety
    into a boolean somebody has to remember to read.

    The reason it matters is asymmetry. A dependency being down is minutes-scale, self-announcing,
    and *is* an inability to serve. A purge that has quietly stopped is hours-scale, silent, and
    breaks a privacy promise — and pulling a perfectly serving container out of load balancing over
    it would fail the deploy's readiness gate, restart the containers, and fix nothing, because the
    thing that stopped is **beat**, which is not in this process and is not a worker.

    So: a stale purge is a **200 with a fact on it**. The operator reads it; the endpoint does not
    act on it.

    **No field has a default**, deliberately, for `PurgeReport`'s reason one layer along: a default
    on a reported fact is how a second construction site quietly publishes a confident `false`.

    Nothing here can carry PII: seven fields, all of them a flag, an instant, a word, a count or an
    exception's *type* (Constitution §8, AC-16).
    """

    scheduled: bool
    """Is the beat entry registered in this deployment? `settings.guest_purge_enabled`."""

    last_run: str | None
    """RFC 3339, UTC, whole-second. `None` when the job has never run *or* Redis was flushed — both
    are worth knowing and the runbook says to investigate either (R-26)."""

    last_run_age_seconds: int | None
    """`None` exactly when `last_run` is."""

    last_outcome: PurgeOutcome | None
    """`"ok"` | `"failed"` | `None`. A `failed` run is still a run that *happened*. The `Literal`
    comes from the heartbeat rather than being restated here, so an unknown word read out of Redis
    arrives as `None` and never as a third outcome the UI has no branch for."""

    overdue: int | None
    """Guest sessions expired-and-present right now, by the purge's own predicate. `None` when the
    count could not be taken, with the reason in `detail` (R-29)."""

    stale: bool
    """`scheduled and (last_run is None or age > GUEST_PURGE_STALE_AFTER_SECONDS)` — see
    `probe_guest_purge` for why `scheduled: false` forces this false (AC-33)."""

    detail: str | None
    """Set only when a field above could not be computed. Exception **types**, never messages."""


async def probe_postgres(engine: AsyncEngine) -> ProbeResult:
    """Round-trip a query. Never `return True` — an unused connection proves nothing."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("probe.postgres.failed", error=type(exc).__name__)
        return ProbeResult("postgres", healthy=False, detail=type(exc).__name__)
    return ProbeResult("postgres", healthy=True)


async def probe_redis(redis_url: str) -> ProbeResult:
    """PING Redis.

    Redis is load-bearing for a user-visible feature here, not merely a cache: it is the Celery
    broker and result backend, so Redis down is "no exports and no purges", not "slower".
    """
    client = create_redis(redis_url)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await client.ping()
    except Exception as exc:
        log.warning("probe.redis.failed", error=type(exc).__name__)
        return ProbeResult("redis", healthy=False, detail=type(exc).__name__)
    finally:
        await client.aclose()
    return ProbeResult("redis", healthy=True)


async def probe_celery(celery_app: Any) -> ProbeResult:
    """Ask the workers to answer.

    `control.ping()` is a **synchronous, network-blocking** call. Calling it directly from this
    coroutine would block the event loop for its full timeout — for every concurrent user, not just
    the one hitting the health endpoint. That is the exact failure this codebase treats as CRITICAL
    (ADR-0005), and a health check that causes an outage would be a memorable way to learn it. Hence
    `asyncio.to_thread`.
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS + 1):
            replies = await asyncio.to_thread(
                celery_app.control.ping, timeout=PROBE_TIMEOUT_SECONDS
            )
    except Exception as exc:
        log.warning("probe.celery.failed", error=type(exc).__name__)
        return ProbeResult("celery", healthy=False, detail=type(exc).__name__)

    if not replies:
        # Not an error — a silence. Which is the whole problem with background workers, and exactly
        # what this probe exists to turn into a red light.
        log.warning("probe.celery.no_workers")
        return ProbeResult("celery", healthy=False, detail="no workers responded")

    return ProbeResult("celery", healthy=True, detail=f"{len(replies)} worker(s)")


async def probe_guest_purge(
    engine: AsyncEngine,
    redis_url: str,
    settings: Settings,
    clock: Clock,
) -> JobStatus:
    """Report the guest purge as a **fact**, never as readiness (ADR-0019, AC-31…AC-34).

    Two independent questions, asked concurrently and degraded independently:

    * **When did it last finish?** — the Redis heartbeat, which also yields `last_outcome`.
    * **How much is overdue right now?** — a `COUNT` over `ix_identity_guest_session_expires_at`,
      asked through `OverdueBacklog` so it is literally the predicate the purge selects on. Not a
      re-derivation: one rule for "expired", read by the job and by this line (AC-34).

    **Either may fail without failing the other, and neither may fail the response.** A timed-out
    count publishes `overdue: null` plus a `detail` and the endpoint still answers 200 (R-29) —
    unless Postgres is genuinely down, in which case `probe_postgres` is already returning 503 for
    its own, correct reason (R-30). Nothing computed here reaches the status code; see `JobStatus`.

    **The backlog is the number to trust, and the heartbeat is the convenience** (ADR-0018 decision
    4). A log line, a run row and a heartbeat can all be written by a job that is not working; a
    falling `overdue` cannot be faked by one. That ordering is why a heartbeat read failure does not
    withhold the count, and vice versa.

    **`scheduled: false` forces `stale: false`** (AC-33). Staleness is a judgement about a schedule
    that is *supposed* to be firing, and `guest_purge_enabled` ships false until a hand rehearsal on
    real data earns the flip. An alarm that is on for that entire window is an alarm nobody reads,
    and by the time it means something nobody will notice it change. `scheduled: false` is itself
    the honest signal, and the UI renders it in words rather than as a colour.

    **"I could not ask" is not "it has never run".** `RedisPurgeHeartbeat.read` raises when Redis is
    unreachable — deliberately, unlike `record`, which swallows — precisely so that this probe can
    tell the two apart. It cannot express the difference in `last_run`, which is `null` either way,
    so it expresses it in `detail`. The truth table in AC-33 is followed literally (`last_run` null
    with the schedule on is `stale: true`) because a pinned truth table with an exception in it is a
    truth table nobody can check; the `detail` is what tells the operator which reading applies, and
    in that scenario `probe_redis` is answering 503 one key over anyway.

    Args:
        engine: the API's engine. A short-lived session is opened on it for the count and closed
            before this returns; nothing here holds a connection between polls.
        redis_url: built into a client per call and closed in a `finally`, exactly as `probe_redis`
            does — `/health/ready` must not leave a pool behind on a path that can fail.
        settings: read for `guest_purge_enabled` and `guest_retention_hours` only.
        clock: the whole-second `Clock`. `last_run_age_seconds` is a subtraction of two instants and
            `datetime.now()` in here would make the age untestable (CLAUDE.md, *Tests*).
    """
    # Deferred for the reason `purge_command.py` writes out at its own copy of this import:
    # `tasks/app.py` builds the Celery application at import and carries two startup refusals of its
    # own. This module deliberately touches no Celery — `probe_celery` takes the app as `Any` rather
    # than importing it — and a health probe is the last place to add an import that can refuse to
    # load. The constant is still *that* one, not a local copy: it is derived from the beat
    # interval, so halving the interval cannot leave this bound three times too generous.
    from tailorcraft.infrastructure.tasks.app import GUEST_PURGE_STALE_AFTER_SECONDS

    scheduled = settings.guest_purge_enabled
    now = clock.now()

    (heartbeat, heartbeat_detail), (overdue, overdue_detail) = await asyncio.gather(
        _read_purge_heartbeat(redis_url),
        _count_overdue_guest_sessions(engine, settings, clock),
    )

    age_seconds = int((now - heartbeat.at).total_seconds()) if heartbeat is not None else None
    stale = scheduled and (age_seconds is None or age_seconds > GUEST_PURGE_STALE_AFTER_SECONDS)
    details = [d for d in (heartbeat_detail, overdue_detail) if d is not None]

    return JobStatus(
        scheduled=scheduled,
        last_run=_as_rfc3339(heartbeat.at) if heartbeat is not None else None,
        last_run_age_seconds=age_seconds,
        last_outcome=heartbeat.outcome if heartbeat is not None else None,
        overdue=overdue,
        stale=stale,
        # Joined rather than "first one wins": two different things can be unreachable at once, and
        # an operator reading one cause while a second is hidden is how a partial outage reads as a
        # simple one.
        detail="; ".join(details) if details else None,
    )


async def _read_purge_heartbeat(redis_url: str) -> tuple[PurgeHeartbeat | None, str | None]:
    """The heartbeat, or `(None, <why>)`.

    Returns a pair rather than raising because the caller must be able to publish the *other* field
    regardless. The `None, None` case is the honest never-run one (R-26); `None, detail` is "I could
    not ask", which is a different fact and gets said out loud.
    """
    client = create_redis(redis_url)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            return await RedisPurgeHeartbeat(client).read(), None
    except Exception as exc:
        # A floor, not an allow-list: this is a health endpoint, and the set of ways a Redis client
        # can fail on a box nobody has looked at is not a set anybody has enumerated. `Exception`,
        # never `BaseException` — a cancelled request must still cancel.
        #
        # `error_type` and nothing else, here and below. The heartbeat hash holds no PII by
        # construction, but formatting an exception's *message* into a log line is the habit that
        # stops being safe the day somebody adds a field (Constitution §8).
        log.warning(_GUEST_PURGE_FAILED_EVENT, field="last_run", error_type=type(exc).__name__)
        return None, f"last_run: {type(exc).__name__}"
    finally:
        await client.aclose()


async def _count_overdue_guest_sessions(
    engine: AsyncEngine, settings: Settings, clock: Clock
) -> tuple[int | None, str | None]:
    """The backlog, or `(None, <why>)` — R-29's degraded field, never a degraded response.

    Its **own** timeout, separate from the heartbeat's: a huge backlog making the `COUNT` slow (R-31)
    must not also cost the `last_run` an operator is reading it beside. If this is ever measured
    above 20 ms p95 the count gets capped (`LIMIT 10001` plus an `overdue_capped` flag) — a decision
    already taken in ADR-0019's consequences rather than a surprise at 2 a.m.
    """
    # Deferred to call time for `deps.py`'s documented reason: the adapter's module builds `Table`
    # objects and reads mapped attributes at *import*, and this module is imported by the router,
    # which `tests/conftest.py` reaches long before its session-scoped mapping fixture runs.
    from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
        SqlAlchemyExpiredGuestData,
    )

    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            factory = create_session_factory(engine)
            async with factory() as session:
                backlog = OverdueBacklog(
                    SqlAlchemyExpiredGuestData(session),
                    clock,
                    # Constructed here rather than passed in, because the window is a *reading* of
                    # one setting and `RetentionWindow` is where that reading lives. An `int` handed
                    # around is how the purge, the cookie and this line drift apart.
                    RetentionWindow(settings.guest_retention_hours),
                )
                return await backlog.count(), None
    except Exception as exc:
        log.warning(_GUEST_PURGE_FAILED_EVENT, field="overdue", error_type=type(exc).__name__)
        return None, f"overdue: {type(exc).__name__}"


def _as_rfc3339(at: datetime) -> str:
    """Whole-second RFC 3339 in UTC, with `Z` rather than `+00:00` (AC-31).

    `astimezone(UTC)` rather than an assumption: the heartbeat's parser guarantees the instant is
    *aware*, not that it is UTC, and a `+02:00` heartbeat rendered verbatim would publish a correct
    instant in a form the contract does not name. `timespec="seconds"` is the `Clock`'s whole-second
    contract restated at the boundary, so a test double that lies about it cannot get a microsecond
    field into a value this endpoint echoes.
    """
    return at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
