"""Account erasure takes every card (slice 3.1, T21 RED): AC-28 and T-34.

Three claims, each with its control in this file:

- **`POST /api/auth/delete-account`** removes the user's cards by the FK cascade (the other user's
  card stays), the `retention.account_erased` line carries `tracked_applications`, and the
  still-valid access token is then **401 `not_signed_in` from every board route** — the part that
  needs the T23 handlers (a skeleton answers 500), so this is where the red lives.
- **`erase-account --dry-run`** prints `; tracking: N tracked application(s)` **after 2.3's
  unchanged prefix** and deletes nothing.
- **`erase-account`** removes the cards and prints the segment after the history segment.

The CLI half is test-after in disguise: `count_account` / `EraseAccount` landed with T16/T17, so those
tests pass on arrival. They are here because AC-28 names them, and they are the regression guard for
"the dry-run's tracking segment sits after 2.3's prefix byte-identically".

The CLI tests seed **committed** rows through the session-scoped engine: `erase_account` opens its own
engine and cannot see the rolled-back test transaction. Users are deleted at teardown.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.retention import erase_account_command
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    A_PASSWORD,
    DELETE_ACCOUNT_URL,
    Account,
    assert_test_database,
    error_code,
    new_client,
    register,
)
from tests.api.tracking_support import (
    ME_BOARD,
    ME_TRACKED,
    card_count,
    card_count_on,
    card_row,
    seed_card,
)
from tests.integration.tracking.support import seed_user


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Delete-account passes a limiter that lives in Redis."""


# --- POST /api/auth/delete-account ---------------------------------------------------------------------


async def test_ac28_deleting_the_account_takes_every_card_and_the_token_is_then_401_on_every_route(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = await register(client, settings)
    other = await register(client, settings)
    at = clock.now() - timedelta(minutes=10)
    first = await seed_card(session, settings, account, at=at)
    second = await seed_card(session, settings, account, at=at)
    others = await seed_card(session, settings, other, at=at)
    assert await card_count(session, account.user_id.value) == 2

    with caplog.at_level(logging.INFO):
        deleted = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": A_PASSWORD},
            headers={**account.headers, "Origin": settings.public_base_url},
        )

    assert deleted.status_code == 204, deleted.text
    assert await card_row(session, first.card_id) is None
    assert await card_row(session, second.card_id) is None
    assert await card_row(session, others.card_id) is not None, "another user's card stays"
    (line,) = [
        json.loads(r.getMessage())
        for r in caplog.records
        if "retention.account_erased" in r.getMessage()
    ]
    assert line["tracked_applications"] == 2, line

    # The access token still verifies for up to 15 minutes; every board route must now refuse it.
    after = [
        await client.get(ME_BOARD, headers=account.headers),
        await client.post(
            ME_TRACKED, json={"tailoring_run_id": str(first.run_id)}, headers=account.headers
        ),
        await client.put(
            first.stage_url, json={"stage": "applied", "version": 1}, headers=account.headers
        ),
        await client.put(
            first.title_url, json={"title": "x", "version": 1}, headers=account.headers
        ),
        await client.delete(first.url, headers=account.headers),
    ]
    assert [r.status_code for r in after] == [401, 401, 401, 401, 401], [r.text for r in after]
    assert {error_code(r) for r in after} == {"not_signed_in"}


# --- erase-account (CLI) -------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    """`erase_account` is called directly, never through `run_from_cli`, so nothing else here
    configures structlog (test_erase_account_cli.py's identical fixture, same reason)."""
    configure_logging(settings)


async def _committed_account_with_cards(
    engine: AsyncEngine, settings: Settings, clock: FixedClock, *, cards: int
) -> tuple[UUID, list[UUID]]:
    at = clock.now() - timedelta(minutes=10)
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        user_id = await seed_user(seeding, clock, f"qa31-erase-{uuid4().hex}@example.com")
        account = Account(token="", owner=UserOwner(user_id))
        ids = [(await seed_card(seeding, settings, account, at=at)).card_id for _ in range(cards)]
    return user_id.value, ids


async def _drop_user(engine: AsyncEngine, user_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})


async def test_ac28_the_dry_run_prints_the_tracking_segment_after_the_unchanged_prefix_and_deletes_nothing(
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert_test_database(settings)
    user_id, _ = await _committed_account_with_cards(engine, settings, clock, cards=2)
    try:
        exit_code = await erase_account_command.erase_account(
            settings, user_id=UserId(user_id), dry_run=True
        )

        out = capsys.readouterr().out
        assert exit_code == erase_account_command.EXIT_OK
        assert (
            f"would erase account {user_id}: 2 saved CV(s), "  # one CV per seeded history entry
        ) in out
        assert "(dry run); history: 2 tailoring run(s), 2 job posting(s), 0 export job(s); " in out
        assert out.strip().endswith("; tracking: 2 tracked application(s)"), out
        assert await card_count_on(engine, user_id) == 2, "a dry run deletes nothing"
    finally:
        await _drop_user(engine, user_id)


async def test_ac28_erase_account_removes_the_cards_and_reports_them_after_the_history_segment(
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert_test_database(settings)
    user_id, _ = await _committed_account_with_cards(engine, settings, clock, cards=3)
    try:
        with caplog.at_level(logging.INFO):
            exit_code = await erase_account_command.erase_account(
                settings, user_id=UserId(user_id), dry_run=False
            )

        out = capsys.readouterr().out
        assert exit_code == erase_account_command.EXIT_OK, caplog.text
        assert out.strip().endswith("; tracking: 3 tracked application(s)"), out
        assert " file(s) in all; tracking: 3 tracked application(s)" in out
        assert await card_count_on(engine, user_id) == 0
        (line,) = [
            json.loads(r.getMessage())
            for r in caplog.records
            if "retention.account_erased" in r.getMessage()
        ]
        assert line["tracked_applications"] == 3, line
    finally:
        await _drop_user(engine, user_id)
