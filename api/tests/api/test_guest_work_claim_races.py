"""`POST /api/me/guest-work/claim` against the requests that race it, over HTTP on `concurrent_app`
(a real session and connection per request) — slice 2.4, T23; AC-15(a), AC-16, AC-17; technical plan
§0.6. AC-13, AC-14 and AC-15(b) are use-case-level and live in
`tests/integration/retention/test_claim_races_purge_and_erasure.py`; AC-18 … AC-20 (the worker's
race) in `tests/api/test_guest_work_claim_in_flight.py`.

Each test stages the interleaving the AC names and **proves from `pg_stat_activity` that the
overlap happened** (`claim_race_support.wait_for_lock_waiter`): a request whose statement is waiting
on a lock is not a request that has not been scheduled yet. Everything is committed, so every test
deletes the user and the guest session it made (the cascades take the rest) and uses a per-test
upload volume.

- **AC-15(a).** A holder takes the user row `FOR UPDATE` (`files_of_account`, uncommitted). The
  claim's first `UPDATE` waits on the user FK check; the holder then erases the user and commits. The
  claim is **401 `not_signed_in`**, every guest row is still guest-owned, the session is present, and
  no cookie was cleared.
- **AC-16.** Two requests, one session. The first to reach `transfer` is **held there** until the
  second is observed waiting on `SELECT … FOR UPDATE identity_guest_session`; one answers 200 with the
  counts, the other 200 with zeros; the rows moved once. The gate raises if the second never waits,
  so a claim that stopped locking is red, not a lucky interleaving.
- **AC-17.** A holder performs the claim's `lock_session` + `transfer` uncommitted. A guest upload /
  posting / run request / export request is sent; its `INSERT` is observed waiting on the session
  row; the holder commits. The write is **401 `guest_session_expired`**, creates no row, and whatever
  file it had already written is reclaimed by the real orphan sweep while every claimed file stays.

**Mutation record (T23)** — applied to `src/` by hand, the named tests watched going red, the source
restored byte-exact (`git diff --stat -- src` empty):

- `.with_for_update()` removed from `SqlAlchemyGuestWorkClaim.lock_session`: AC-16 red, `assert (200, 500)
  == (200, 200)` (the held request's gate raises because the second claim never waited); 1 failed, 5 passed.
- `.with_for_update()` removed from `SqlAlchemyAccountData.files_of_account`: AC-15(a) red, `no backend was
  ever waiting on a lock in a statement containing ('update intake_base_cv',)`; 1 failed, 5 passed.
- The `_GUEST_FK` branch (`if violated_constraint(exc) == _GUEST_FK`) disabled in all four guest
  repositories: all four AC-17 cases red, `assert 503 == 401` (`db.request_failed`); 4 failed, 2 passed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.domain.identity.claim import ClaimedGuestWork
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner
from tailorcraft.infrastructure.persistence.identity.guest_work_claim import (
    SqlAlchemyGuestWorkClaim,
)
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.me_support import SAMPLE_CV, Entry, error_code, new_client
from tests.integration.claim_race_support import (
    OWNED_TABLES,
    owned_row_counts,
    pinned_session,
    session_exists,
    wait_for_lock_waiter,
)

CLAIM_URL = "/api/me/guest-work/claim"
_STEP_TIMEOUT = 20.0


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The register and claim limiters live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path)
    try:
        yield w
    finally:
        await w.cleanup()


# --- AC-15 (a): erasure holds the user row; the claim waits on the FK, then fails -------------


async def test_ac15a_a_claim_waiting_on_an_erasing_users_row_is_401_and_moves_nothing(
    world: World,
) -> None:
    """AC-15(a). The holder runs `files_of_account` (the user row `FOR UPDATE`, uncommitted). The
    claim's first `UPDATE` is observed waiting; the holder then deletes the user and commits. The
    claim is 401 `not_signed_in`; every guest row is still guest-owned, the session is present, and
    no `Set-Cookie` cleared the guest cookie.

    **Mutation (T23), restored byte-exact:** `.with_for_update()` removed from
    `SqlAlchemyAccountData.files_of_account` — the claim never waits behind the holder."""
    account = await world.account()
    async with new_client(world.app) as browser:
        guest, _, _ = await world.guest_with_work(browser)
        before = await owned_row_counts(world.engine, guest=guest.guest_session_id)

        async with pinned_session(world.engine) as holder_session:
            accounts = SqlAlchemyAccountData(holder_session)
            await accounts.files_of_account(account.user_id)  # user row FOR UPDATE, uncommitted
            claim_task = asyncio.create_task(browser.post(CLAIM_URL, headers=account.headers))
            try:
                waiting = await wait_for_lock_waiter(world.engine, "update intake_base_cv")
                assert waiting
                assert not claim_task.done(), "the claim answered while the user row was locked"
            finally:
                assert await accounts.delete_account(account.user_id) is True
                await holder_session.commit()
            response: Response = await asyncio.wait_for(claim_task, _STEP_TIMEOUT)

        assert response.status_code == 401, response.text
        assert error_code(response) == "not_signed_in"
        assert response.headers.get_list("set-cookie") == [], "the guest cookie must stay"
        assert await owned_row_counts(world.engine, guest=guest.guest_session_id) == before
        assert sum(before.values()) > 0
        assert await session_exists(world.engine, guest.guest_session_id)


# --- AC-16: two claims of one session ---------------------------------------------------------


async def test_ac16_two_claims_of_one_session_are_one_200_with_counts_and_one_200_with_zeros(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-16. Both requests carry the same bearer and the same `tc_guest` cookie. Whichever reaches
    `transfer` first is **held there** until the other is observed waiting on `SELECT … FOR UPDATE
    identity_guest_session`; the gate raises if that never happens, so a claim that stopped locking
    answers 500 here instead of passing by luck of scheduling.

    **Mutation (T23), restored byte-exact:** `.with_for_update()` removed from
    `SqlAlchemyGuestWorkClaim.lock_session` — the second claim never waits; the gate raises and the
    held request answers 500."""
    account = await world.account()
    async with new_client(world.app) as tab_one, new_client(world.app) as tab_two:
        guest, _, _ = await world.guest_with_work(tab_one)
        tab_two.cookies.set(COOKIE_NAME, tab_one.cookies.get(COOKIE_NAME) or "")
        before = await owned_row_counts(world.engine, guest=guest.guest_session_id)

        original = SqlAlchemyGuestWorkClaim.transfer
        entered: list[str] = []

        async def _gated(
            self: SqlAlchemyGuestWorkClaim, session_id: GuestSessionId, user_id: UserId
        ) -> ClaimedGuestWork:
            entered.append("transfer")
            if len(entered) == 1:
                await wait_for_lock_waiter(
                    world.engine, "identity_guest_session", "for update", within=8.0
                )
            return await original(self, session_id, user_id)

        monkeypatch.setattr(SqlAlchemyGuestWorkClaim, "transfer", _gated)

        first, second = await asyncio.wait_for(
            asyncio.gather(
                tab_one.post(CLAIM_URL, headers=account.headers),
                tab_two.post(CLAIM_URL, headers=account.headers),
            ),
            _STEP_TIMEOUT,
        )

    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    bodies = sorted((first.json(), second.json()), key=lambda body: -body["base_cvs"])
    assert bodies[0] == {
        "base_cvs": before["intake_base_cv"],
        "job_postings": before["posting_job_posting"],
        "tailoring_runs": before["tailoring_run"],
        "export_jobs": before["export_job"],
        "working_copies_dropped": 0,
    }
    assert bodies[1] == dict.fromkeys(bodies[0], 0)
    assert entered == ["transfer"], "exactly one request may reach the transfer"
    assert await owned_row_counts(world.engine, user=account.user_id) == before
    assert await owned_row_counts(world.engine, guest=guest.guest_session_id) == dict.fromkeys(
        OWNED_TABLES, 0
    )


# --- AC-17: a guest write waiting on the session row --------------------------------------------


async def _upload(client: AsyncClient, entry: Entry) -> Response:
    return await client.post(
        "/api/base-cvs", files={"file": ("racing.txt", SAMPLE_CV, "text/plain")}
    )


async def _posting(client: AsyncClient, entry: Entry) -> Response:
    return await client.post(
        "/api/job-postings", json={"source": "pasted", "text": "A racing posting. " * 20}
    )


async def _run(client: AsyncClient, entry: Entry) -> Response:
    return await client.post(
        "/api/tailoring-runs",
        json={
            "base_cv_id": str(entry.cv_id.value),
            "job_posting_id": str(entry.posting_id.value),
        },
    )


async def _export(client: AsyncClient, entry: Entry) -> Response:
    return await client.post(
        f"/api/tailoring-runs/{entry.run_id.value}/exports",
        json={"document": "cv", "format": "docx"},
    )


@pytest.mark.parametrize(
    ("write", "table", "orphans_before_sweep"),
    [
        (_upload, "intake_base_cv", 1),
        (_posting, "posting_job_posting", 0),
        (_run, "tailoring_run", 0),
        (_export, "export_job", 0),
    ],
    ids=["upload", "posting", "run", "export"],
)
async def test_ac17_a_guest_write_waiting_on_the_session_row_is_401_after_the_claim_and_leaves_nothing(
    world: World,
    write: Callable[[AsyncClient, Entry], Coroutine[Any, Any, Response]],
    table: str,
    orphans_before_sweep: int,
) -> None:
    """AC-17. The holder performs the claim's `lock_session` + `transfer` and does **not** commit.
    The guest write is sent; its `INSERT INTO <table>` is observed waiting (its FK check needs the
    session row `FOR KEY SHARE`, which the holder's `FOR UPDATE` refuses); the holder commits; the
    write answers **401 `guest_session_expired`**. No row was created (the user's counts are exactly
    what the claim moved, the guest's are zero), and the real orphan sweep leaves the volume holding
    exactly the claimed files — whatever the refused write had already put there is gone.

    **Mutation (T23), restored byte-exact:** the `GuestSessionNotFound` translation removed from the
    matching repository's `add` — that case answers 503 `db.request_failed` and this test goes red on `assert 503 == 401`."""
    account = await world.account()
    async with new_client(world.app) as browser:
        guest, entry, token_hash = await world.guest_with_work(browser)
        before = await owned_row_counts(world.engine, guest=guest.guest_session_id)
        files_before = world.files()
        assert files_before, (
            "the claimed files must be on the volume, or the sweep proof is vacuous"
        )

        async with pinned_session(world.engine) as holder_session:
            claim = SqlAlchemyGuestWorkClaim(holder_session)
            locked = await claim.lock_session(token_hash)
            assert locked is not None
            await claim.transfer(locked.id, account.user_id)  # uncommitted: the lock is held
            write_task = asyncio.create_task(write(browser, entry))
            try:
                waiting = await wait_for_lock_waiter(world.engine, f"insert into {table}")
                assert waiting
                assert not write_task.done(), "the write answered while the session row was locked"
            finally:
                await holder_session.commit()  # the claim is durable; the session row is gone
            response: Response = await asyncio.wait_for(write_task, _STEP_TIMEOUT)

    assert response.status_code == 401, response.text
    assert error_code(response) == "guest_session_expired"
    assert await owned_row_counts(world.engine, user=account.user_id) == before
    assert await owned_row_counts(world.engine, guest=guest.guest_session_id) == dict.fromkeys(
        OWNED_TABLES, 0
    )
    assert not await session_exists(world.engine, guest.guest_session_id)

    # Only an upload writes a file before its INSERT (the CV bytes are stored first): that file is the
    # orphan the spec names. The other three write none, so there is nothing for the sweep to prove.
    assert len(world.files() - files_before) == orphans_before_sweep, sorted(world.files())

    # The sweep, for real: every file older than any grace, only what no row references goes.
    far_future = FixedClock(datetime.now(UTC).replace(microsecond=0) + timedelta(days=10))
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as sweep_session:
        sweep = ReclaimOrphanedFiles(
            LocalOrphanFileScanner(world.root),
            SqlAlchemyExpiredGuestData(sweep_session),
            LocalFileStore(world.root),
            far_future,
            RetentionWindow(hours=24),
            timedelta(hours=1),
        )
        await sweep()
    assert world.files() == files_before, (
        "after the sweep the volume must hold exactly the claimed files: a refused write's orphan "
        f"must be gone and no claimed file lost (before={sorted(files_before)}, "
        f"after={sorted(world.files())})"
    )
