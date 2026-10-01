"""The claim against the purge and against account erasure, on **two real connections** (slice 2.4,
T23; AC-13 PROOF, AC-14, AC-15(b); technical plan §0.5, §0.6). AC-15(a) is over HTTP and lives in
`tests/api/test_guest_work_claim_races.py`.

Written after the code, **against the spec's AC text** (feature-spec "Concurrency"), not against the
adapters: each test stages the interleaving the AC names, proves from `pg_stat_activity` or from the
recorded order that the overlap really happened, and then asserts on the database and the volume
(R-42: never on the report the code under test produced, except where the AC itself names it —
`sessions_skipped = 1`).

- **AC-13 (PROOF), claim first.** `list_expired` collects session S's keys, then a claim of S commits
  on another connection, then the purge's `delete_session(S)` runs. A decorator over the real
  committing adapter runs the claim *inside* `list_expired`'s return, which is the moment the spec
  names ("lists → claim commits → purge deletes"). The purge must answer `False`, unlink nothing,
  and count `sessions_skipped = 1`.
- **AC-14, purge first.** A wrapper holds the purge's `DELETE` uncommitted (the row lock is held);
  the claim's `SELECT … FOR UPDATE` is observed **waiting** in `pg_stat_activity`; only then is the
  purge released. The claim finds no session: zeros, nothing claimed; the purge unlinks as before.
- **AC-15(b), claim first, then erasure.** The claim's `UPDATE`s are held uncommitted (they hold the
  user row `FOR KEY SHARE`); erasure's `FOR UPDATE` on the user row is observed waiting; the claim
  commits; erasure then takes the claimed rows **and their files**.

Every connection is pinned with `lock_timeout` (`claim_race_support.pinned_session`); every task is
awaited under `asyncio.wait_for`, so a regression fails in seconds and names a lock.

**Mutation record (T23).** Each mutation was applied to `src/` by hand, the named test watched
going red, and the source restored byte-exact (`git diff --stat -- src` empty):

- `SqlAlchemyExpiredGuestData.delete_session` returns `True` unconditionally (AC-13's mutation):
  `test_ac13_...` red, `assert ['listed', 'claimed', 'delete_session=True'] == ['listed', 'claimed', 'delete_session=False']`
  (1 failed, 2 passed).
- `.with_for_update()` removed from `SqlAlchemyGuestWorkClaim.lock_session`: `test_ac14_...` red — the
  claim no longer queues behind the purge's `DELETE`, and Postgres answers `DeadlockDetectedError`
  on `DELETE FROM identity_guest_session` (1 failed, 2 passed).
- `.with_for_update()` removed from `SqlAlchemyAccountData.files_of_account`: `test_ac15b_...` red —
  `LockNotAvailableError` (55P03, our `lock_timeout`) on `DELETE FROM identity_user`; the erasure
  read the user unlocked and then could not delete behind the claim (1 failed, 2 passed).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.application.identity.claim_guest_work import ClaimGuestWork
from tailorcraft.application.retention.erase_account import EraseAccount
from tailorcraft.application.retention.purge_expired_guest_sessions import (
    PurgeExpiredGuestSessions,
)
from tailorcraft.domain.identity.claim import GuestWorkClaimReport
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.retention.ports import ExpiredGuestDataPort
from tailorcraft.domain.retention.value_objects import ExpiringGuestSession, RetentionWindow
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.identity.claim_access import CommittingGuestWorkClaim
from tailorcraft.infrastructure.persistence.identity.guest_work_claim import (
    SqlAlchemyGuestWorkClaim,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.retention.data_access import (
    CommittingAccountData,
    CommittingExpiredGuestDataAdapter,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.claim_race_support import (
    SeededGuest,
    assert_test_database,
    drop_rows,
    new_user,
    owned_row_counts,
    pinned_session,
    seed_guest_with_work,
    session_exists,
    wait_for_lock_waiter,
)

_STEP_TIMEOUT = 15.0
_ONE_OF_EACH = dict.fromkeys(
    ("intake_base_cv", "posting_job_posting", "tailoring_run", "export_job"), 1
)
_NONE_OF_EACH = dict.fromkeys(_ONE_OF_EACH, 0)


def _present(root: Path, ref: FileRef) -> bool:
    return (root / ref.key).is_file()


def _purge(
    data: ExpiredGuestDataPort, files: LocalFileStore, clock: FixedClock
) -> PurgeExpiredGuestSessions:
    return PurgeExpiredGuestSessions(
        data=data, files=files, clock=clock, window=RetentionWindow(hours=24), batch_limit=100
    )


async def _claim(
    engine: AsyncEngine,
    files: LocalFileStore,
    clock: FixedClock,
    user_id: UserId,
    token_hash: str,
) -> GuestWorkClaimReport:
    """The real `ClaimGuestWork` over the real committing adapter, on its own pinned connection."""
    async with pinned_session(engine) as session:
        claim = ClaimGuestWork(
            SqlAlchemyUserRepository(session),
            CommittingGuestWorkClaim(SqlAlchemyGuestWorkClaim(session), session),
            files,
            clock,
        )
        return await claim(user_id, token_hash)


class _OnlyThisSession:
    """Narrows `list_expired` to the session under test (the test database may hold other expired
    sessions — assertions are scoped to ids this test created) and runs `hook` once the keys
    are collected, **before** the purge's first `delete_session` — the moment AC-13 names."""

    def __init__(
        self, inner: ExpiredGuestDataPort, only: GuestSessionId, events: list[str]
    ) -> None:
        self._inner = inner
        self._only = only
        self._events = events
        self.hook: object | None = None

    async def count_expired(self, as_of: datetime) -> int:
        return await self._inner.count_expired(as_of)

    async def list_expired(self, as_of: datetime, limit: int) -> Sequence[ExpiringGuestSession]:
        listed = [
            c for c in await self._inner.list_expired(as_of, limit) if c.session_id == self._only
        ]
        assert len(listed) == 1, "the session under test was not listed as expired"
        self._events.append("listed")
        if self.hook is not None:
            await self.hook()  # type: ignore[operator]
        return listed

    async def delete_session(self, session_id: GuestSessionId) -> bool:
        deleted = await self._inner.delete_session(session_id)
        self._events.append(f"delete_session={deleted}")
        return deleted

    async def which_are_referenced(self, keys: Sequence[FileRef]) -> frozenset[FileRef]:
        return await self._inner.which_are_referenced(keys)


# --- AC-13 (PROOF): the claim commits between the purge's listing and its DELETE ---------------


async def test_ac13_a_claim_committing_between_listing_and_delete_makes_the_purge_skip_and_unlink_nothing(
    settings: Settings, engine: AsyncEngine, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-13 PROOF. Purge clock is past `expires_at`, the claim's clock is before it: the session is
    expired *to the purge* and live *to the claim*, the only way both can act on one row.

    The order is recorded and asserted (`listed`, `claimed`, `delete_session=False`), and the claim's
    own report is asserted non-zero, so a claim that did nothing cannot let this pass.

    **Mutation (T23), restored byte-exact:** `SqlAlchemyExpiredGuestData.delete_session` returning
    `True` unconditionally — the purge unlinks the claimed CV and export files; red on the recorded
    order (`delete_session=True`). See the module docstring for the observed failure."""
    assert_test_database(settings)
    files = LocalFileStore(tmp_path)
    started = clock.now()
    seeded = await seed_guest_with_work(engine, files, started_at=started)
    user_id = await new_user(engine, clock)
    claim_clock = FixedClock(started + timedelta(hours=1))
    purge_clock = FixedClock(started + timedelta(hours=25))
    events: list[str] = []
    claimed: list[GuestWorkClaimReport] = []
    try:
        async with pinned_session(engine) as purge_session:
            data = _OnlyThisSession(
                CommittingExpiredGuestDataAdapter(
                    SqlAlchemyExpiredGuestData(purge_session), purge_session
                ),
                seeded.guest,
                events,
            )

            async def _claim_now() -> None:
                claimed.append(
                    await asyncio.wait_for(
                        _claim(engine, files, claim_clock, user_id, seeded.token_hash),
                        _STEP_TIMEOUT,
                    )
                )
                events.append("claimed")

            data.hook = _claim_now
            report = await asyncio.wait_for(_purge(data, files, purge_clock)(), _STEP_TIMEOUT)

        assert events == ["listed", "claimed", "delete_session=False"], events
        assert (claimed[0].base_cvs, claimed[0].tailoring_runs, claimed[0].export_jobs) == (1, 1, 1)
        assert report.sessions_skipped == 1
        assert report.sessions_deleted == 0
        assert report.files_unlinked == 0
        for ref in seeded.files:
            assert _present(tmp_path, ref), "claimed files were unlinked by the purge"
        assert await owned_row_counts(engine, user=user_id) == _ONE_OF_EACH
        assert not await session_exists(engine, seeded.guest)
    finally:
        await drop_rows(engine, guests=[seeded.guest], users=[user_id])


# --- AC-14: the purge's DELETE holds the lock; the claim waits, then finds no session ----------


class _HoldingDelete:
    """The purge's data port with `delete_session` held open: the `DELETE` has run (its row lock is
    taken) and is **not committed** until `release` is set — then it commits and answers."""

    def __init__(
        self, inner: ExpiredGuestDataPort, session: AsyncSession, only: GuestSessionId
    ) -> None:
        self._inner = inner
        self._session = session
        self._only = only
        self.deleted = asyncio.Event()
        self.release = asyncio.Event()

    async def count_expired(self, as_of: datetime) -> int:
        return await self._inner.count_expired(as_of)

    async def list_expired(self, as_of: datetime, limit: int) -> Sequence[ExpiringGuestSession]:
        return [
            c for c in await self._inner.list_expired(as_of, limit) if c.session_id == self._only
        ]

    async def delete_session(self, session_id: GuestSessionId) -> bool:
        deleted = await self._inner.delete_session(session_id)  # the lock is held from here
        self.deleted.set()
        await asyncio.wait_for(self.release.wait(), _STEP_TIMEOUT)
        await self._session.commit()
        return deleted

    async def which_are_referenced(self, keys: Sequence[FileRef]) -> frozenset[FileRef]:
        return await self._inner.which_are_referenced(keys)


async def test_ac14_a_claim_waiting_on_the_purges_delete_finds_no_session_and_claims_nothing(
    settings: Settings, engine: AsyncEngine, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-14. The claim runs its real `lock_session` while the purge's `DELETE` of the session is
    uncommitted: the claim's `SELECT … FOR UPDATE` is observed **waiting** (`pg_stat_activity`, not a
    sleep), is still pending when observed, and completes only after the purge commits — with zero
    counts. The purge, having removed the row, unlinks the files as it always did.

    The claim's clock is *before* `expires_at` so that the only reason it finds nothing is the lock.

    **Mutation (T23), restored byte-exact:** `.with_for_update()` removed from
    `SqlAlchemyGuestWorkClaim.lock_session` — red (observed: `DeadlockDetectedError`; see above)."""
    assert_test_database(settings)
    files = LocalFileStore(tmp_path)
    started = clock.now()
    seeded = await seed_guest_with_work(engine, files, started_at=started)
    user_id = await new_user(engine, clock)
    claim_clock = FixedClock(started + timedelta(hours=1))
    purge_clock = FixedClock(started + timedelta(hours=25))
    try:
        async with pinned_session(engine) as purge_session:
            data = _HoldingDelete(
                SqlAlchemyExpiredGuestData(purge_session), purge_session, seeded.guest
            )
            purge_task = asyncio.create_task(_purge(data, files, purge_clock)())
            try:
                await asyncio.wait_for(data.deleted.wait(), _STEP_TIMEOUT)
                claim_task = asyncio.create_task(
                    _claim(engine, files, claim_clock, user_id, seeded.token_hash)
                )
                try:
                    waiting = await wait_for_lock_waiter(
                        engine, "identity_guest_session", "for update"
                    )
                    assert waiting
                    assert not claim_task.done(), "the claim finished while the purge held the lock"
                finally:
                    data.release.set()
                report = await asyncio.wait_for(purge_task, _STEP_TIMEOUT)
                claim_report = await asyncio.wait_for(claim_task, _STEP_TIMEOUT)
            finally:
                data.release.set()
                for task in (purge_task,):
                    if not task.done():
                        task.cancel()

        assert claim_report == GuestWorkClaimReport.nothing()
        assert await owned_row_counts(engine, user=user_id) == _NONE_OF_EACH
        assert await owned_row_counts(engine, guest=seeded.guest) == _NONE_OF_EACH
        assert not await session_exists(engine, seeded.guest)
        assert report.sessions_deleted == 1
        for ref in seeded.files:
            assert not _present(tmp_path, ref), "the purge's own files must still be unlinked"
    finally:
        await drop_rows(engine, guests=[seeded.guest], users=[user_id])


# --- AC-15 (b): the claim commits first, then erasure takes the claimed rows and files ---------


async def test_ac15b_erasure_waiting_behind_a_claim_takes_the_claimed_rows_and_their_files(
    settings: Settings, engine: AsyncEngine, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-15(b). The claim's transfer is **uncommitted** (its `UPDATE`s hold the user row `FOR KEY
    SHARE` through the FK check); `EraseAccount`'s `FOR UPDATE` on that user row is observed
    **waiting**; the claim commits; erasure then collects the **claimed** rows and files and removes
    them: nothing is left in any table, and both files are gone from the volume.

    The claim is held at the adapter (`lock_session` + `transfer`, then an explicit commit) rather than
    through `ClaimGuestWork`, which would commit before the erasure could be staged behind it.

    **Mutation (T23), restored byte-exact:** `.with_for_update()` removed from
    `SqlAlchemyAccountData.files_of_account` — red (observed: `LockNotAvailableError`; see above)."""
    assert_test_database(settings)
    files = LocalFileStore(tmp_path)
    seeded: SeededGuest = await seed_guest_with_work(engine, files, started_at=clock.now())
    user_id = await new_user(engine, clock)
    try:
        async with pinned_session(engine) as claim_session, pinned_session(engine) as erase_session:
            claim = SqlAlchemyGuestWorkClaim(claim_session)
            locked = await claim.lock_session(seeded.token_hash)
            assert locked is not None
            moved = await claim.transfer(locked.id, user_id)  # uncommitted: user row key-shared
            assert (moved.base_cvs, moved.tailoring_runs, moved.export_jobs) == (1, 1, 1)

            erase = EraseAccount(
                CommittingAccountData(SqlAlchemyAccountData(erase_session), erase_session), files
            )
            erase_task = asyncio.create_task(erase(user_id))
            try:
                waiting = await wait_for_lock_waiter(engine, "identity_user", "for update")
                assert waiting
                assert not erase_task.done(), "erasure finished while the claim held the user row"
            finally:
                await claim_session.commit()  # the claim's rows are durable; its locks are released
            report = await asyncio.wait_for(erase_task, _STEP_TIMEOUT)

        assert report.base_cvs == 1
        assert (report.tailoring_runs, report.job_postings, report.export_jobs) == (1, 1, 1)
        assert report.files == 2
        assert report.files_unlinked == 2
        assert report.unlink_failures == ()
        for ref in seeded.files:
            assert not _present(tmp_path, ref), "a claimed file survived the account's erasure"
        assert await owned_row_counts(engine, user=user_id) == _NONE_OF_EACH
        assert await owned_row_counts(engine, guest=seeded.guest) == _NONE_OF_EACH
    finally:
        await drop_rows(engine, guests=[seeded.guest], users=[user_id])
