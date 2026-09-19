"""The guest purge's mutual-exclusion lock: `SET NX PX` over Redis, with a token and a
compare-and-delete release.

One purge at a time, because two concurrent runs are wasted database work rather than a correctness
problem (the job is idempotent by construction — a deleted session is no longer expired-and-present,
a second `DELETE` affects zero rows, and `FileStorePort.delete` is `missing_ok`). That framing is the
whole reason this lock is *advisory* and fails open: see `acquire`.

**Three states, not two.** "I hold it", "somebody else holds it" and "I could not ask" are three
different facts with three different entry-point behaviours (R-7, R-8), and a boolean can express two
of them at most. Collapsing "Redis is down" into "the lock is held" would make a Redis outage skip
every purge, which is the exact inversion of the direction ADR-0018 decision 6 chose — and it would
make the CLI exit 3 ("a purge is already running") for a purge that nothing is running. AC-20 names
that trap directly: **a run that did nothing because the lock was held must not exit 0.**

Not a domain port (ADR-0018 decision 8) — see this package's `__init__` for the argument, which is
`infrastructure/rate_limit.py`'s, transferred.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from enum import Enum
from typing import Final, cast

import redis.asyncio as aioredis
import structlog

log = structlog.get_logger(__name__)

# The one key. Namespaced like the heartbeat's, so `retention:guest_purge:*` is the whole of this
# job's operational state in Redis and a flush of it is one visible thing rather than two.
PURGE_LOCK_KEY: Final = "retention:guest_purge:lock"

# Compare-and-delete: release the lock **only if we still hold it**. A `GET` followed by a `DEL` is
# two round trips with a gap in the middle, and the gap is exactly where a run that overran its TTL
# deletes its successor's lock — after which two purges run believing they are alone, which is the
# one thing this module exists to prevent. The token makes the comparison possible and the script
# makes it atomic; either alone is decoration.
_RELEASE_SCRIPT: Final = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# Compare-and-**refresh**, for the identical reason. A bare `PEXPIRE` extends whatever lock is at the
# key, and if ours expired mid-run that is somebody else's — a refresh would hand a successor's lock
# an extra TTL on every batch, from a run that no longer holds anything.
_REFRESH_SCRIPT: Final = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


class PurgeLockOutcome(Enum):
    """What asking for the lock produced. Three members, and each one has a different entry-point
    behaviour attached to it (see `PurgeLockHold.may_run`)."""

    ACQUIRED = "acquired"
    """We hold it. Run, and release it in the `finally` this module's context manager provides."""

    HELD_BY_ANOTHER = "held_by_another"
    """A live run (R-8) or a dead one inside its TTL (R-9) holds it. **Do not run.** The Celery task
    logs `outcome=skipped` and returns *without raising* — a tick colliding with a long manual run is
    normal operation, not a fault — while the CLI exits **3**, because an operator who typed the
    command is owed the news that it did nothing."""

    UNAVAILABLE = "unavailable"
    """Redis could not answer. **Run anyway** — see `RedisPurgeLock.acquire` for the argument."""


class PurgeLockHold:
    """The result of asking for the lock, and the handle for refreshing it.

    A small object rather than a bare enum because a holder needs its token to refresh or release,
    and a token that the caller has to carry is a token the caller can pass to the wrong call. The
    context manager owns both; the caller reads `outcome` and calls `refresh`.
    """

    __slots__ = ("_lock", "_token", "outcome")

    def __init__(
        self, outcome: PurgeLockOutcome, lock: RedisPurgeLock | None, token: str | None
    ) -> None:
        self.outcome = outcome
        self._lock = lock
        self._token = token

    @property
    def may_run(self) -> bool:
        """Whether the purge should go ahead.

        **`UNAVAILABLE` says yes**, and that is the fail-open direction stated as a single
        expression, so no caller has to re-derive it from a two-way comparison. Only
        `HELD_BY_ANOTHER` says no.
        """
        return self.outcome is not PurgeLockOutcome.HELD_BY_ANOTHER

    async def refresh(self) -> None:
        """Push the TTL out by another full window, atomically and only if we still hold it.

        Called between batches on a long CLI run (AC-19): a full-volume purge on a box with a real
        backlog can outlast a TTL sized for one Celery tick, and a lock that expires under a run
        still working is a lock that lets a second run start. Never raises and never refreshes
        somebody else's lock — a no-op for every outcome but `ACQUIRED`.
        """
        if self._lock is None or self._token is None:
            return
        await self._lock._refresh(self._token)


class RedisPurgeLock:
    """`SET NX PX` with a random token, released by compare-and-delete.

    `ttl_seconds` is a **required constructor argument with no default**, and the composition roots
    pass `tasks.app.PURGE_LOCK_TTL_SECONDS` — the derived constant (`TASK_TIME_LIMIT_SECONDS + 60`)
    that ADR-0018 decision 7 chose over a setting, so that this slice adds no fourth startup refusal
    that cannot exit the container under `uvicorn --workers N` (AC-30).

    **Why a parameter rather than importing that constant here.** `tasks/app.py` builds the Celery
    application at import (`app = create_celery()`), which reads `Settings` and can raise
    `MisconfiguredSettings` for reasons that have nothing to do with retention — a tailoring or
    export stale window. Importing it from this module would mean that `purge-guests`, a CLI that
    publishes no task and needs no broker, refuses to start because the *tailoring* sweep is
    misconfigured. The lock knows nothing about Celery; the entry points that already do supply the
    number. It is also the testing seam: a test proving the TTL expiry of R-9 passes 1, not 240.
    """

    def __init__(
        self, redis: aioredis.Redis, ttl_seconds: int, *, key: str = PURGE_LOCK_KEY
    ) -> None:
        self._redis = redis
        self._ttl_ms = ttl_seconds * 1000
        self._key = key

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[PurgeLockHold]:
        """Take the lock for the duration of the block, and release it in `finally` if we took it.

        The context manager is the intended entry point precisely because the release is the part
        that must not be forgotten: a purge that raises on its third session must still give the lock
        back, or the next hourly tick finds it held by a run that is no longer running and the TTL
        becomes the only recovery (R-9).

        It yields for **all three** outcomes — a caller that is not going to run still needs to know
        why — so the block begins with `if not hold.may_run`.
        """
        hold = await self.acquire()
        try:
            yield hold
        finally:
            await self._release(hold)

    async def acquire(self) -> PurgeLockHold:
        """Try to take the lock. Never raises.

        **This fails OPEN: Redis unreachable means the purge still runs** (R-7). The argument, at the
        point of the code, because a reader who meets only one half of it will make the two halves
        "consistent" and will pick the wrong one half the time:

        - The cost of a **skipped** purge is a broken privacy promise. FR-6 says guest data lives at
          most 24 hours, and PII sitting on disk past its window is not recoverable by apologising
          afterwards. A lock outage that stops purges is an outage that silently rewrites the
          retention policy.
        - The cost of an **overlapping** purge is duplicated work on an idempotent job. Two runs
          deleting the same already-deleted session is a no-op by the port's contract (AC-15, R-19),
          and a second `FileStorePort.delete` is `missing_ok` by contract too. It is wasted database
          work, bounded and ours.

        **The orphan sweep's database cross-check fails CLOSED, and it is the same decision pointing
        the other way** — `application/retention/reclaim_orphaned_files.py` writes the other half at
        length, and ADR-0018 decision 6 holds both. There, the thing at risk is *a file*: deleting
        somebody's CV because we could not ask whether a row still points at it is the one
        irreversible mistake this tool can make, so it deletes nothing and exits non-zero. The rule
        underneath both is one sentence: **put a mechanism's failure on the side whose loss is
        recoverable.** Duplicated work in one case, a stranger's CV in the other. Applying one
        direction to both would either stop purges when Redis blinks or delete files when Postgres
        does.

        The token is `secrets.token_hex`, not the process id or the hostname: two workers on one box
        would collide on either, and the token's whole job is to be unguessable by the *next* holder
        of the same key.
        """
        token = secrets.token_hex(16)
        try:
            acquired = await self._redis.set(self._key, token, nx=True, px=self._ttl_ms)
        except Exception as exc:
            # `Exception`, never `BaseException` — a cancelled run must still cancel. Broad rather
            # than `RedisError` because every failure here has the same answer: we do not know, so we
            # proceed. An allow-list would only decide which unknowns crash the purge instead.
            log.warning("retention.lock_unavailable", error_type=type(exc).__name__)
            return PurgeLockHold(PurgeLockOutcome.UNAVAILABLE, None, None)

        if not acquired:
            return PurgeLockHold(PurgeLockOutcome.HELD_BY_ANOTHER, None, None)
        return PurgeLockHold(PurgeLockOutcome.ACQUIRED, self, token)

    async def _release(self, hold: PurgeLockHold) -> None:
        """Give the lock back, but only if the key still holds *our* token. Never raises.

        A failed release is logged and swallowed: the lock expires on its own within
        `ttl_seconds`, so the worst case is one skipped tick (R-9) — the same bounded cost the
        fail-open direction already accepts. Raising here would turn a successful purge into a failed
        one over the cleanup of an advisory lock.
        """
        if hold.outcome is not PurgeLockOutcome.ACQUIRED or hold._token is None:
            return
        try:
            # redis-py types `eval` as `ResponseT` — `Awaitable[T] | T`, one signature for both the
            # sync and the async client — so a bare `await` does not type-check. The script's own
            # return value is discarded on purpose: "it was not ours to delete" and "we deleted it"
            # are the same outcome for a caller that is finished either way.
            await cast(
                "Awaitable[object]", self._redis.eval(_RELEASE_SCRIPT, 1, self._key, hold._token)
            )
        except Exception as exc:
            log.warning("retention.lock_release_failed", error_type=type(exc).__name__)

    async def _refresh(self, token: str) -> None:
        """Back `PurgeLockHold.refresh`. Never raises, for `_release`'s reason."""
        try:
            await cast(
                "Awaitable[object]",
                self._redis.eval(_REFRESH_SCRIPT, 1, self._key, token, str(self._ttl_ms)),
            )
        except Exception as exc:
            log.warning("retention.lock_refresh_failed", error_type=type(exc).__name__)
