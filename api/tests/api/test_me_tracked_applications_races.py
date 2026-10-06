"""The tracking races, over HTTP, on real committed rows (slice 3.1, T21 RED): AC-19, AC-26's
"a card deleted between the handler's read and its write", T-17, T-18, T-19, T-20 (concurrent), T-21,
T-31 (concurrent).

Every test runs on `concurrent_app` — a real session per request — because the shared `app`
fixture's one SAVEPOINT-bound session cannot tell "committed" from "issued", and two concurrent
requests through it die with `IllegalStateChangeError` (CLAUDE.md, 2.2). Rows are real; each test
deletes its users at the end (the cascades take the rest).

**Staged at the moment the spec names (H-33).** A delete that commits before the request is sent is
a clean 404, not the race. The row is removed on **another connection, inside the repository call
the spec names** — `save` for a card deleted between the handler's read and its write, `add` for a
user or a run erased between authorization and insert — by a wrapper that commits the competing
write and then delegates to the real method unchanged. Each such test also asserts that the
competing write really landed mid-request, so a refactor that moves the seam fails loudly instead of
quietly testing a clean 404.

Against the T20 skeleton every handler raises `NotImplementedError`, so each test is red on its first
status assertion (`assert 500 == 201`), after the staging guard has nothing to say.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.repositories.tracking.tracked_application import (
    SqlAlchemyTrackedApplicationRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    Account,
    assert_test_database,
    build_concurrent_app,
    error_body,
    error_code,
    new_client,
    register,
    seed_entry,
)
from tests.api.tracking_support import ME_TRACKED, Seeded, card_count_on, seed_card

_STEP_TIMEOUT = 20.0


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every write passes a limiter that lives in Redis."""


@pytest.fixture
def concurrent_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    return build_concurrent_app(settings, engine, password_hasher)


@pytest_asyncio.fixture
async def accounts(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine
) -> AsyncIterator[list[Account]]:
    """Users created by a test; deleted at teardown (the cascades take their runs and cards)."""
    assert_test_database(settings)
    created: list[Account] = []
    yield created
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '8000ms'"))
        for account in created:
            await conn.execute(
                text("DELETE FROM identity_user WHERE id = :i"), {"i": account.user_id.value}
            )


async def _account(concurrent_app: FastAPI, settings: Settings, accounts: list[Account]) -> Account:
    async with new_client(concurrent_app) as setup:
        account = await register(setup, settings)
    accounts.append(account)
    return account


async def _card(
    engine: AsyncEngine, settings: Settings, clock: FixedClock, account: Account
) -> Seeded:
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        return await seed_card(seeding, settings, account, at=clock.now() - timedelta(minutes=10))


async def _bare_run(
    engine: AsyncEngine, settings: Settings, clock: FixedClock, account: Account
) -> UUID:
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        entry = await seed_entry(
            seeding,
            settings,
            account.owner,
            at=clock.now() - timedelta(minutes=10),
            ready_formats=(),
        )
        return entry.run_id.value


# --- AC-19 / T-19: two tracks of one run ---------------------------------------------------------------


async def test_ac19_two_concurrent_tracks_of_one_run_are_one_201_and_one_409_naming_the_winner(
    concurrent_app: FastAPI,
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    accounts: list[Account],
) -> None:
    account = await _account(concurrent_app, settings, accounts)
    run_id = await _bare_run(engine, settings, clock, account)
    body = {"tailoring_run_id": str(run_id)}

    async def track() -> Response:
        async with new_client(concurrent_app) as c:
            return await c.post(ME_TRACKED, json=body, headers=account.headers)

    first, second = await asyncio.wait_for(asyncio.gather(track(), track()), _STEP_TIMEOUT)

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [201, 409], [first.text, second.text]
    winner, loser = (first, second) if first.status_code == 201 else (second, first)
    assert error_code(loser) == "application_already_tracked"
    assert error_body(loser)["tracked_application_id"] == winner.json()["id"]
    assert await card_count_on(engine, account.user_id.value) == 1


# --- T-18: the account is erased while the track request is in flight ----------------------------------


async def test_t18_a_track_racing_an_erasure_is_401_not_signed_in_and_leaves_no_card(
    concurrent_app: FastAPI,
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    accounts: list[Account],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The user row is deleted on another connection **between the use case's read of the user and
    its run, and the card's INSERT** — the INSERT's FK check then fails on
    `fk_tracking_application_user_id_identity_user`, which the repository translates to
    `UserNotFound`: 401 `not_signed_in`, never a 503."""
    account = await _account(concurrent_app, settings, accounts)
    run_id = await _bare_run(engine, settings, clock, account)
    original_add = SqlAlchemyTrackedApplicationRepository.add
    erased_mid_request: list[bool] = []

    async def _add_after_an_erasure(
        self: SqlAlchemyTrackedApplicationRepository, card: TrackedApplication
    ) -> None:
        async with engine.begin() as other:
            gone = await other.execute(
                text("DELETE FROM identity_user WHERE id = :i"), {"i": account.user_id.value}
            )
            erased_mid_request.append(gone.rowcount == 1)
        await original_add(self, card)

    monkeypatch.setattr(SqlAlchemyTrackedApplicationRepository, "add", _add_after_an_erasure)

    with caplog.at_level(logging.INFO):
        async with new_client(concurrent_app) as c:
            response = await asyncio.wait_for(
                c.post(ME_TRACKED, json={"tailoring_run_id": str(run_id)}, headers=account.headers),
                _STEP_TIMEOUT,
            )

    assert response.status_code == 401, response.text
    assert erased_mid_request == [True], "the erasure must land between the read and the INSERT"
    assert error_code(response) == "not_signed_in"
    assert await card_count_on(engine, account.user_id.value) == 0
    assert "identity.user_missing" in caplog.text


# --- T-17: the history entry is deleted while the track request is in flight ---------------------------


async def test_t17_a_track_racing_a_history_entry_deletion_is_404_run_not_found_and_leaves_no_card(
    concurrent_app: FastAPI,
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    accounts: list[Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run is deleted on another connection after authorization and before `add` — the card's
    own INSERT then succeeds (there is no FK on the run) and the post-INSERT `FOR KEY SHARE` on the
    run finds nothing: the INSERT is rolled back to its SAVEPOINT (AC-18 b over HTTP)."""
    account = await _account(concurrent_app, settings, accounts)
    run_id = await _bare_run(engine, settings, clock, account)
    original_add = SqlAlchemyTrackedApplicationRepository.add
    deleted_mid_request: list[bool] = []

    async def _add_after_a_run_deletion(
        self: SqlAlchemyTrackedApplicationRepository, card: TrackedApplication
    ) -> None:
        async with engine.begin() as other:
            gone = await other.execute(
                text("DELETE FROM tailoring_run WHERE id = :r"), {"r": run_id}
            )
            deleted_mid_request.append(gone.rowcount == 1)
        await original_add(self, card)

    monkeypatch.setattr(SqlAlchemyTrackedApplicationRepository, "add", _add_after_a_run_deletion)

    async with new_client(concurrent_app) as c:
        response = await asyncio.wait_for(
            c.post(ME_TRACKED, json={"tailoring_run_id": str(run_id)}, headers=account.headers),
            _STEP_TIMEOUT,
        )

    assert response.status_code == 404, response.text
    assert deleted_mid_request == [True], "the deletion must land between the read and the INSERT"
    assert error_code(response) == "tailoring_run_not_found"
    assert await card_count_on(engine, account.user_id.value) == 0


# --- T-21 / AC-26: the card is deleted between the handler's read and its write ------------------------


@pytest.mark.parametrize(
    ("suffix", "body"),
    [
        ("/stage", {"stage": "applied", "version": 1}),
        ("/title", {"title": "Globex", "version": 1}),
    ],
    ids=["move", "retitle"],
)
async def test_t21_a_write_racing_a_committed_untrack_is_409_with_a_null_current_version_then_404(
    concurrent_app: FastAPI,
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    accounts: list[Account],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    suffix: str,
    body: dict[str, object],
) -> None:
    """H-33's shape. The card has **already been read** by the use case when its row is deleted on
    another connection; the write's `UPDATE … WHERE version` then matches no row → 409
    `tracked_application_version_conflict` with `current_version: null`, the row stays gone (the
    losing write must not resurrect it), and a fresh request is 404. The line
    `tracking.concurrent_modification` names the card's id."""
    account = await _account(concurrent_app, settings, accounts)
    card = await _card(engine, settings, clock, account)
    original_save = SqlAlchemyTrackedApplicationRepository.save
    deleted_mid_request: list[bool] = []

    async def _save_after_a_committed_delete(
        self: SqlAlchemyTrackedApplicationRepository, target: TrackedApplication
    ) -> None:
        async with engine.begin() as other:
            gone = await other.execute(
                text("DELETE FROM tracking_application WHERE id = :i"), {"i": card.card_id}
            )
            deleted_mid_request.append(gone.rowcount == 1)
        await original_save(self, target)

    monkeypatch.setattr(
        SqlAlchemyTrackedApplicationRepository, "save", _save_after_a_committed_delete
    )

    with caplog.at_level(logging.INFO):
        async with new_client(concurrent_app) as c:
            raced = await asyncio.wait_for(
                c.put(card.url + suffix, json=body, headers=account.headers), _STEP_TIMEOUT
            )

    assert raced.status_code == 409, raced.text
    assert deleted_mid_request == [True], "the delete must land between the read and the write"
    assert error_code(raced) == "tracked_application_version_conflict"
    assert error_body(raced)["current_version"] is None
    assert await card_count_on(engine, account.user_id.value) == 0, "the loser must not write back"
    assert "tracking.concurrent_modification" in caplog.text
    assert str(card.card_id) in caplog.text

    monkeypatch.undo()
    async with new_client(concurrent_app) as c:
        again = await c.put(card.url + suffix, json=body, headers=account.headers)
    assert again.status_code == 404, again.text
    assert error_code(again) == "tracked_application_not_found"


# --- T-20 (concurrent): two moves from the same version ------------------------------------------------


async def test_t20_two_concurrent_moves_from_one_version_are_one_200_and_one_409_never_a_5xx(
    concurrent_app: FastAPI,
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    accounts: list[Account],
) -> None:
    account = await _account(concurrent_app, settings, accounts)
    card = await _card(engine, settings, clock, account)

    async def move(stage: str) -> Response:
        async with new_client(concurrent_app) as c:
            return await c.put(
                card.stage_url, json={"stage": stage, "version": 1}, headers=account.headers
            )

    first, second = await asyncio.wait_for(
        asyncio.gather(move("applied"), move("interviewing")), _STEP_TIMEOUT
    )

    assert sorted([first.status_code, second.status_code]) == [200, 409], [first.text, second.text]
    loser = first if first.status_code == 409 else second
    winner = second if loser is first else first
    assert error_code(loser) == "tracked_application_version_conflict"
    assert winner.json()["version"] == 2
    async with engine.connect() as probe:
        row = (
            await probe.execute(
                text("SELECT stage, version FROM tracking_application WHERE id = :i"),
                {"i": card.card_id},
            )
        ).one()
    assert (row[0], row[1]) == (winner.json()["stage"], 2)


# --- T-31 (concurrent): two untracks ------------------------------------------------------------------


async def test_t31_two_concurrent_untracks_are_one_204_and_one_404_never_a_5xx(
    concurrent_app: FastAPI,
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    accounts: list[Account],
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = await _account(concurrent_app, settings, accounts)
    card = await _card(engine, settings, clock, account)

    async def untrack() -> Response:
        async with new_client(concurrent_app) as c:
            return await c.delete(card.url, headers=account.headers)

    with caplog.at_level(logging.INFO):
        first, second = await asyncio.wait_for(asyncio.gather(untrack(), untrack()), _STEP_TIMEOUT)

    assert sorted([first.status_code, second.status_code]) == [204, 404], [first.text, second.text]
    loser = first if first.status_code == 404 else second
    assert error_code(loser) == "tracked_application_not_found"
    assert await card_count_on(engine, account.user_id.value) == 0
