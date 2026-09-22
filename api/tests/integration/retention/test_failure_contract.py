"""T33 — the failure-contract rows no other module covers.

Twenty-three of the `R-` rows are already asserted where their behaviour lives: R-1/R-3/R-4/R-5,
R-12/R-13/R-14/R-15 and R-7/R-8 in `test_purge_cli.py`; R-33…R-39 in the two orphan-sweep modules;
R-18 in `tests/unit/retention/`; R-25/R-26/R-29/R-32 in `tests/api/test_health.py`. This module adds
the four that were genuinely uncovered, and records why the rest are not tests.

**Rows that are deliberately not tested here, each with its reason** — an untested row should be an
argued decision rather than an oversight a reviewer has to notice:

* **R-2** (Postgres dies mid-batch). The observable half — a refused write is contained, the earlier
  commits stand, the batch continues — is `test_purge_database.py`'s SAVEPOINT test. Killing a live
  connection mid-loop would test asyncpg's reconnection, not this slice's contract.
* **R-6** (crash between the committed delete and the unlink). This is the crash window ADR-0006 §2
  *chose*; there is nothing to assert about it except that the survivor is recoverable, which is
  exactly what the orphan-sweep tests already prove.
* **R-10** (beat stopped) and **R-11** (worker stopped). R-10's row says it plainly: *nowhere —
  nothing raises and nothing logs*. The signal is `last_run` ageing into `stale`, which the
  `/health/ready` truth table covers, and a climbing `overdue`, which the probe test covers. R-11 is
  the existing Celery probe.
* **R-20 … R-24** (racing a live request). Every one resolves to behaviour earlier slices already
  own and already test — the FK on an in-flight insert, X-33's `export.job_missing`, X-43/X-47's
  download states, the 401 resolver. This slice adds no code to those paths, so a test here would
  assert another slice's contract.
* **R-41** (a leftover Redis lock between tests) is proved by running the suite twice, not by a
  test — which is the point of the property.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention.lock import PurgeLockOutcome, RedisPurgeLock
from tailorcraft.infrastructure.settings import Settings

# --- R-40 / AC-39: the guard that exists because of the 1.4 incident ------------------------------


def _assert_test_database(settings: Settings) -> None:
    """The guard every deleting test in this package calls before its first statement.

    Copied deliberately rather than imported, because `test_purge_cli.py`'s copy is the one under
    test in spirit: if this module imported that one's helper, a test asserting "the guard fires"
    would be asserting the behaviour of the very function it borrowed, and a future edit that
    weakened one copy would leave the other silently unexercised.
    """
    url = settings.database_url
    assert "_test" in url, f"refusing to run a deleting test against {url.rsplit('@', 1)[-1]}"


def test_the_test_database_guard_fires_on_a_non_test_url(settings: Settings) -> None:
    """R-40 / AC-39. The guard is only worth having if it refuses.

    CLAUDE.md's 1.4 incident is this exact shape: a cleanup script built its own engine from
    `settings.database_url` under `APP_ENV=test` — which still returns the **dev** URL, because only
    `conftest.py` swaps in `test_database_url` — and `DELETE FROM tailoring_run; DELETE FROM
    identity_guest_session` emptied dev through these very cascades. That happened *before* the
    purge existed. This slice is the one whose whole purpose is `DELETE`, so the guard belongs to it
    more than to any other.

    A guard nobody has watched refuse is a belief, not a control.
    """
    dev_shaped = settings.model_copy(
        update={
            "database_url": "postgresql+asyncpg://tailorcraft:tailorcraft@postgres:5432/tailorcraft"
        }
    )

    with pytest.raises(AssertionError, match="refusing to run a deleting test"):
        _assert_test_database(dev_shaped)


def test_the_test_database_guard_passes_on_the_real_test_url(settings: Settings) -> None:
    """The other half: it must not refuse the URL every test in this package actually uses, or it
    would be a guard that is always on and therefore always routed around."""
    _assert_test_database(settings)
    assert "_test" in settings.database_url


# --- R-16: the window is frozen at `start`, so changing the setting moves nothing ------------------


def test_changing_the_retention_window_does_not_move_an_existing_session() -> None:
    """R-16, asserted by construction rather than by running a purge.

    `expires_at` is computed **once**, at `GuestSession.start`, from the `ttl_hours` in force then.
    Nothing re-reads the setting afterwards, so shortening the window does not expire old sessions
    early and lengthening it does not resurrect deleted ones.

    This row is in the contract because *"we changed the setting, why is nothing happening"* is the
    support question the design guarantees — and the answer is a property of the aggregate, not of
    the purge. Asserting it against two sessions started under two different windows is the whole
    proof: it needs no database, no clock beyond a fixed instant, and no purge run.
    """
    at = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    started_under_24h = GuestSession.start(
        id=GuestSessionId(uuid4()), token_hash="a" * 64, at=at, ttl_hours=24
    )
    started_under_1h = GuestSession.start(
        id=GuestSessionId(uuid4()), token_hash="b" * 64, at=at, ttl_hours=1
    )

    assert started_under_24h.expires_at == at + timedelta(hours=24)
    assert started_under_1h.expires_at == at + timedelta(hours=1)

    # The purge's predicate reads the column, never the setting. Two hours after `at`, the session
    # started under the one-hour window is expired and the other is not — and no later change to
    # `guest_retention_hours` can alter either verdict, because neither aggregate consults it again.
    two_hours_later = at + timedelta(hours=2)
    assert started_under_1h.is_expired(two_hours_later) is True
    assert started_under_24h.is_expired(two_hours_later) is False


# --- R-9: a lock held by a dead run frees itself when its TTL expires -----------------------------


async def test_a_lock_held_by_a_dead_run_frees_itself_when_the_ttl_expires(
    settings: Settings, clear_redis: None
) -> None:
    """R-9. The holder was SIGKILLed, so nothing will ever release the key — the TTL is the only
    thing that can, and it is why the lock has one.

    Driven with a one-second TTL rather than the production 240 s, which is exactly the seam
    `RedisPurgeLock` exists to offer: the TTL is a constructor argument and not a read of
    `PURGE_LOCK_TTL_SECONDS`, so this test can assert the mechanism in a second instead of four
    minutes.

    The worst case this bounds is **one skipped hourly tick**, which is why an advisory lock with a
    TTL is proportionate here and a correctness mechanism would not be (ADR-0018 decision 6).
    """
    redis = create_redis(settings.redis_url)
    try:
        dead_holder = RedisPurgeLock(redis, ttl_seconds=1)
        successor = RedisPurgeLock(redis, ttl_seconds=1)

        abandoned = await dead_holder.acquire()
        assert abandoned.outcome is PurgeLockOutcome.ACQUIRED

        # Nothing released it — the holder is gone. A second run must be refused, not fail open:
        # "another run holds this" and "Redis could not answer" are different outcomes with
        # different exit codes (AC-20), and collapsing them is what a boolean would have done.
        blocked = await successor.acquire()
        assert blocked.outcome is PurgeLockOutcome.HELD_BY_ANOTHER
        assert blocked.may_run is False

        await asyncio.sleep(1.2)

        recovered = await successor.acquire()
        assert recovered.outcome is PurgeLockOutcome.ACQUIRED
        # No explicit release: `clear_redis` flushes, and the point of this row is precisely that
        # nothing released the *first* hold either.
    finally:
        await redis.aclose()


# --- R-19: two concurrent purges are safe, because the lock is not what makes them safe ------------


async def test_two_concurrent_lock_holders_cannot_both_acquire(
    settings: Settings, clear_redis: None
) -> None:
    """R-19's mutual-exclusion half: `SET NX` means exactly one of two racing acquirers wins.

    The *idempotency* half — that the loser doing the work anyway would still be harmless, because
    a second `DELETE` affects zero rows and `FileStorePort.delete` is `missing_ok` — is asserted in
    `test_purge_expired_guest_sessions.py` (AC-15) and in `test_purge_database.py`. That split is
    deliberate: **the lock is advisory, and idempotency is what actually makes concurrency safe.**
    If this test were the only one, it would read as though correctness depended on the lock, and
    then R-7's fail-open — Redis down means run anyway — would look like a bug instead of a
    decision.
    """
    redis = create_redis(settings.redis_url)
    try:
        first = RedisPurgeLock(redis, ttl_seconds=30)
        second = RedisPurgeLock(redis, ttl_seconds=30)

        outcomes = await asyncio.gather(first.acquire(), second.acquire())
        acquired = [h for h in outcomes if h.outcome is PurgeLockOutcome.ACQUIRED]
        refused = [h for h in outcomes if h.outcome is PurgeLockOutcome.HELD_BY_ANOTHER]

        assert len(acquired) == 1, "SET NX admitted two holders"
        assert len(refused) == 1

        # And the loser leaving its block must not free the winner's lock. Driven through `hold()`
        # rather than the private release, because the context manager *is* the public entry point
        # and its `finally` is the thing that has to get this right: a run that released a key it
        # never took would hand a successor's lock away, which is the race the token closes.
        async with second.hold() as loser:
            assert loser.outcome is PurgeLockOutcome.HELD_BY_ANOTHER
        assert await redis.exists("retention:guest_purge:lock") == 1, (
            "a refused holder released a lock it never took"
        )
    finally:
        await redis.aclose()


def test_the_fixed_clock_is_whole_second_by_contract() -> None:
    """A guard on the guard: every retention test dates its fixtures from `FixedClock`, and the
    `Clock` port is whole-second **by contract** (ADR-0007) because a value carrying microseconds in
    but not out turns every equality assertion into a coin flip. `FixedClock` asserts that rather
    than trusting it; this pins that it still does."""
    with pytest.raises(ValueError, match="whole-second by contract"):
        FixedClock(datetime(2026, 9, 20, 12, 0, 0, 123_456, tzinfo=UTC))
