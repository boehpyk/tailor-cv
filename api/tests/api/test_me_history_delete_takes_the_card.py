"""AC-24 over 2.3's route, unchanged: `DELETE /api/me/tailoring-runs/{id}` on a **tracked** history entry
(slice 3.1, T19, test-after — the change is in a persistence adapter, T17; the route is 2.3's).

The entry's card goes with it and the deletion is **committed with the run** — proved on
`concurrent_app` (a real session per request) from a **separate connection** at the moment the file
store's `delete` is called, 2.2's AC-27 technique: on the shared `app` fixture every statement is
visible on the one connection whether or not a commit ran, so only a separate connection can tell
"committed" from "issued". Each claim has its control in the same file:

- the tracked entry: 204, the card **gone and committed**, the run, jobs and files gone as in 2.3,
  and the `retention.history_entry_erased` line gains `tracked_application_deleted: true`;
- **no other card touched** — a second tracked entry of the same user and another user's card;
- an **untracked** entry: 204, the same line with `tracked_application_deleted: false`, and the
  other cards still there — 2.3's behaviour with one more field.

(AC-24's last sentence — *untracking leaves the history entry, its documents and its export files
byte-identical* — needs the untrack route, which T20/T23 build; it is T21's. Its repository half,
that `remove` touches only the card, is in `test_tracked_application_repository.py`.)

Rows are real and committed; the user (and, by the cascades, everything else) is deleted at teardown.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.repositories.tracking.tracked_application import (
    SqlAlchemyTrackedApplicationRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    Account,
    Entry,
    assert_test_database,
    build_concurrent_app,
    new_client,
    register,
    seed_entry,
)
from tests.integration.tracking.support import a_card


@pytest.fixture
def concurrent_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    return build_concurrent_app(settings, engine, password_hasher)


@pytest_asyncio.fixture
async def client(concurrent_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(concurrent_app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The sign-in used to get a bearer goes through the login limiter, which lives in Redis."""


async def _seed_entry_with_card(
    engine: AsyncEngine,
    settings: Settings,
    clock: FixedClock,
    account: Account,
    *,
    tracked: bool,
) -> tuple[Entry, TrackedApplicationId | None]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        entry = await seed_entry(
            seeding, settings, account.owner, at=clock.now(), ready_formats=(ExportFormat.PDF,)
        )
        if not tracked:
            return entry, None
        repo = SqlAlchemyTrackedApplicationRepository(seeding)
        card = a_card(
            account.user_id, clock.now(), run_id=entry.run_id, card_id=repo.next_identity()
        )
        card_id = card.id
        await repo.add(card)
        await seeding.commit()
        return entry, card_id


async def _cards(engine: AsyncEngine, *card_ids: TrackedApplicationId | None) -> list[bool]:
    """For each id, whether its row exists — read on a fresh connection."""
    async with engine.connect() as probe:
        return [
            bool(
                (
                    await probe.execute(
                        text("SELECT count(*) FROM tracking_application WHERE id = :i"),
                        {"i": card_id.value},
                    )
                ).scalar_one()
            )
            for card_id in card_ids
            if card_id is not None
        ]


async def _drop_users(engine: AsyncEngine, *accounts: Account) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '8000ms'"))
        for account in accounts:
            await conn.execute(
                text("DELETE FROM identity_user WHERE id = :i"), {"i": account.user_id.value}
            )


def _erased_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if "retention.history_entry_erased" in r.getMessage()
    ]


async def test_ac24_deleting_a_tracked_entry_is_204_and_the_card_is_gone_and_committed_with_the_run(
    concurrent_app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert_test_database(settings)
    account = await register(client, settings)
    other = await register(client, settings)
    entry, card_id = await _seed_entry_with_card(engine, settings, clock, account, tracked=True)
    _, sibling_card = await _seed_entry_with_card(engine, settings, clock, account, tracked=True)
    _, others_card = await _seed_entry_with_card(engine, settings, clock, other, tracked=True)
    assert await _cards(engine, card_id, sibling_card, others_card) == [True, True, True]
    export_path = settings.upload_dir / entry.jobs[0].storage_ref.key
    assert export_path.exists()
    seen_at_unlink: list[list[bool]] = []
    original_delete = LocalFileStore.delete

    async def _checking_delete(self: LocalFileStore, ref: FileRef) -> None:
        seen_at_unlink.append(await _cards(engine, card_id))
        await original_delete(self, ref)

    try:
        with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.INFO):
            mp.setattr(LocalFileStore, "delete", _checking_delete)
            response = await client.delete(entry.run_url, headers=account.headers)

        assert response.status_code == 204, response.text
        assert response.content == b""
        assert seen_at_unlink == [[False]], (
            "the card must already be committed-gone, on a separate connection, when the first file "
            "is unlinked — rows committed, then files"
        )
        assert await _cards(engine, card_id) == [False]
        assert await _cards(engine, sibling_card, others_card) == [True, True], (
            "no other card is touched"
        )
        assert not export_path.exists(), "2.3's behaviour for the entry's files is unchanged"
        (line,) = _erased_lines(caplog)
        assert line["tracked_application_deleted"] is True
        assert line["tailoring_run_id"] == str(entry.run_id.value)
        assert line["user_id"] == str(account.user_id.value)
    finally:
        await _drop_users(engine, account, other)


async def test_ac24_deleting_an_untracked_entry_is_204_reports_no_card_and_touches_none(
    client: AsyncClient,
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The control: 2.3's route and 2.3's line for an entry nobody tracked, plus the one new field
    reading `false`, and the user's *other*, tracked entry untouched."""
    assert_test_database(settings)
    account = await register(client, settings)
    untracked, _ = await _seed_entry_with_card(engine, settings, clock, account, tracked=False)
    _, tracked_card = await _seed_entry_with_card(engine, settings, clock, account, tracked=True)
    try:
        with caplog.at_level(logging.INFO):
            response = await client.delete(untracked.run_url, headers=account.headers)

        assert response.status_code == 204, response.text
        assert await _cards(engine, tracked_card) == [True]
        (line,) = _erased_lines(caplog)
        assert line["tracked_application_deleted"] is False
        assert line["tailoring_run_id"] == str(untracked.run_id.value)
        assert (line["export_jobs"], line["files_unlinked"], line["files_failed"]) == (1, 1, 0)
        assert line["posting_deleted"] is True
    finally:
        await _drop_users(engine, account)


async def test_ac24_a_deleted_entry_stays_deleted_and_its_card_does_not_come_back(
    client: AsyncClient,
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
) -> None:
    """A second delete is 2.3's 404 and creates, finds or reports nothing about a card."""
    assert_test_database(settings)
    account = await register(client, settings)
    entry, card_id = await _seed_entry_with_card(engine, settings, clock, account, tracked=True)
    try:
        first = await client.delete(entry.run_url, headers=account.headers)
        second = await client.delete(entry.run_url, headers=account.headers)

        assert (first.status_code, second.status_code) == (204, 404), (first.text, second.text)
        assert await _cards(engine, card_id) == [False]
    finally:
        await _drop_users(engine, account)
