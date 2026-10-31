"""`POST /api/me/guest-work/claim` and the fit scores it carries, over HTTP on `concurrent_app` (a real
session and connection per request) — slice 5.1, T21, **red first**; AC-34, S-31, plan §3.3.

- **The count.** The response gains `fit_scores`, the number of scores the claim moved. A twin and a
  working copy's score are not counted. (The key is read with `.get`, so a response that lacks it is a
  wrong value, not a `KeyError`.)
- **A twin that was there before the claim** is skipped: 200, the user's row stays, the guest's goes.
- **S-31: a twin that appears *during* the claim.** The user's store of the same pair is uncommitted
  when the claim's `UPDATE` reaches that pair: the claim's `NOT EXISTS` cannot see it, the `UPDATE`
  waits on the unique index, and the store then commits. The claim fails on `uq_scoring_fit_score_user_pair`,
  which the adapter translates to `GuestWorkClaimConflict`: **503, the whole claim rolled back, every
  guest row still guest-owned, the session present, the guest cookie not cleared**; a retry succeeds and
  finds the twin. The overlap is proven from `pg_stat_activity` (the claim's `UPDATE` observed waiting),
  not assumed from a sleep, and the staging is at the `UPDATE` the spec names: the claim's earlier
  statements have already run inside the same transaction when it waits.

The 503's error code is not named by the spec (S-31: "503"), so these tests pin the status and the
envelope only.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.me_support import Account, error_code, new_client
from tests.integration.claim_race_support import (
    OWNED_TABLES,
    owned_row_counts,
    pinned_session,
    session_exists,
    wait_for_lock_waiter,
)
from tests.integration.persistence.fit_score_rows import insert_score_row, owner_of, score_of

CLAIM_URL = "/api/me/guest-work/claim"
_STEP_TIMEOUT = 20.0
_PAIR = 7


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


async def _commit_score(
    world: World, clock: FixedClock, *, pair: int, score: int = 50, **owner: UUID | None
) -> UUID:
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
        row = await insert_score_row(
            session, scored_at=clock.now(), pair=pair, score=score, **owner
        )
        await session.commit()
    return row


async def _owner_of(world: World, row: UUID) -> tuple[UUID | None, UUID | None] | None:
    async with async_sessionmaker(world.engine)() as session:
        return await owner_of(session, row)


async def _score_of(world: World, row: UUID) -> int | None:
    async with async_sessionmaker(world.engine)() as session:
        return await score_of(session, row)


# --- the count -----------------------------------------------------------------------------------


async def test_the_response_counts_the_fit_scores_the_claim_moved(
    world: World, clock: FixedClock
) -> None:
    account = await world.account()
    async with new_client(world.app) as browser:
        guest, entry, _ = await world.guest_with_work(browser)
        moved = [
            await _commit_score(
                world,
                clock,
                pair=pair,
                guest_session_id=guest.guest_session_id.value,
                base_cv_id=entry.cv_id.value,
            )
            for pair in (1, 2)
        ]

        response = await browser.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json().get("fit_scores") == 2
    assert [await _owner_of(world, row) for row in moved] == [(None, account.user_id.value)] * 2


async def test_the_response_counts_zero_fit_scores_for_a_guest_who_made_none(
    world: World,
) -> None:
    account = await world.account()
    async with new_client(world.app) as browser:
        await world.guest_with_work(browser)

        response = await browser.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json().get("fit_scores") == 0


async def test_a_twin_that_was_there_before_the_claim_is_skipped_and_not_counted(
    world: World, clock: FixedClock
) -> None:
    """AC-34 over HTTP: the user already holds this pair (committed). 200, not a 500; their row is
    untouched; the guest's twin is gone with the session; the count says 0."""
    account = await world.account()
    async with new_client(world.app) as browser:
        guest, entry, _ = await world.guest_with_work(browser)
        users = await _commit_score(
            world, clock, pair=_PAIR, score=90, user_id=account.user_id.value
        )
        twin = await _commit_score(
            world,
            clock,
            pair=_PAIR,
            score=20,
            guest_session_id=guest.guest_session_id.value,
            base_cv_id=entry.cv_id.value,
        )

        response = await browser.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json().get("fit_scores") == 0
    assert await _owner_of(world, users) == (None, account.user_id.value)
    assert await _score_of(world, users) == 90
    assert await _owner_of(world, twin) is None


# --- S-31: the claim collides with a concurrent store --------------------------------------------


@dataclass(frozen=True)
class _Collision:
    """The state after a claim that was held at its fit-score `UPDATE` while the user's own store of
    the same pair committed."""

    browser: AsyncClient
    account: Account
    guest: GuestOwner
    first_claim: Response
    guest_rows_before: dict[str, int]
    guests_score: UUID
    users_score: UUID


@asynccontextmanager
async def _collided(world: World, clock: FixedClock) -> AsyncIterator[_Collision]:
    account = await world.account()
    async with new_client(world.app) as browser:
        guest, entry, _ = await world.guest_with_work(browser)
        before = await owned_row_counts(world.engine, guest=guest.guest_session_id)
        guests_score = await _commit_score(
            world,
            clock,
            pair=_PAIR,
            score=20,
            guest_session_id=guest.guest_session_id.value,
            base_cv_id=entry.cv_id.value,
        )

        async with pinned_session(world.engine) as storing:
            users_score = await insert_score_row(
                storing,
                scored_at=clock.now(),
                pair=_PAIR,
                score=88,
                user_id=account.user_id.value,
            )  # the user's own store, uncommitted: invisible to the claim's NOT EXISTS
            claim = asyncio.create_task(browser.post(CLAIM_URL, headers=account.headers))
            try:
                waiting = await wait_for_lock_waiter(world.engine, "update scoring_fit_score")
                assert waiting
                assert not claim.done(), "the claim answered while the user's store was uncommitted"
            finally:
                await storing.commit()
            first = await asyncio.wait_for(claim, _STEP_TIMEOUT)

        yield _Collision(browser, account, guest, first, before, guests_score, users_score)


async def test_s31_a_claim_that_collides_with_a_concurrent_store_answers_503(
    world: World, clock: FixedClock
) -> None:
    async with _collided(world, clock) as c:
        assert c.first_claim.status_code == 503, c.first_claim.text
        assert error_code(c.first_claim)


async def test_s31_a_collided_claim_rolls_everything_back_and_keeps_the_guest_cookie(
    world: World, clock: FixedClock
) -> None:
    async with _collided(world, clock) as c:
        assert c.first_claim.headers.get_list("set-cookie") == [], "the guest cookie must stay"
        assert (
            await owned_row_counts(world.engine, guest=c.guest.guest_session_id)
            == c.guest_rows_before
        )
        assert sum(c.guest_rows_before.values()) > 0
        assert await owned_row_counts(world.engine, user=c.account.user_id) == dict.fromkeys(
            OWNED_TABLES, 0
        )
        assert await session_exists(world.engine, c.guest.guest_session_id)
        assert await _owner_of(world, c.guests_score) == (c.guest.guest_session_id.value, None)
        assert await _score_of(world, c.users_score) == 88


async def test_s31_a_retry_after_the_collision_finds_the_twin_and_succeeds(
    world: World, clock: FixedClock
) -> None:
    async with _collided(world, clock) as c:
        retry = await c.browser.post(CLAIM_URL, headers=c.account.headers)

        assert retry.status_code == 200, retry.text
        assert retry.json().get("fit_scores") == 0
        assert await owned_row_counts(world.engine, user=c.account.user_id) == (c.guest_rows_before)
        assert not await session_exists(world.engine, c.guest.guest_session_id)
        assert await _owner_of(world, c.guests_score) is None, "the twin goes with the session"
        assert await _owner_of(world, c.users_score) == (None, c.account.user_id.value)
        assert await _score_of(world, c.users_score) == 88
