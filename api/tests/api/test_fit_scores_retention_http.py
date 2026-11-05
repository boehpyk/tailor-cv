"""Fit scores through the routes that delete or show what a user or guest holds (slice 5.1, T23;
AC-37, AC-38, AC-39), over HTTP on a concurrent app (a real, committed session per request).

- **AC-37 / AC-38.** Deleting a saved CV, or a history entry that takes its posting, deletes that
  user's scores over it, from the account's own route **and** from `/admin` (one use case, two seats).
  The store races are in `tests/integration/persistence/test_fit_score_deletion_races.py`.
- **AC-39.** The three admin reads show a count named `fit_scores`, and no score or gap text reaches
  any admin body: the scores here carry a marker string in their gaps, and no admin response may
  contain it. (Read with `.get`, so a count T24 has yet to add is a wrong value, not a `KeyError`.)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.settings import Settings
from tests.api.admin_guest_world import GUESTS, Guest, Who, World, open_world
from tests.api.me_support import ME_BASE_CVS, ME_RUNS
from tests.integration.persistence.fit_score_rows import insert_score_row

ADMIN_USERS = "/api/admin/users"
_MARKER = "ZQX-GAP-TEXT-MARKER"


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """A login in `World.account` passes a limiter that lives in Redis."""


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher, tmp_path: Path
) -> AsyncIterator[World]:
    async with open_world(settings, engine, password_hasher, tmp_path / "uploads") as w:
        yield w


async def _score(
    world: World,
    *,
    user: Who | None = None,
    guest: Guest | None = None,
    cv: UUID | None = None,
    posting: UUID | None = None,
    pair: int = 1,
) -> UUID:
    """One committed score row. Its gaps carry `_MARKER`, which no admin body may repeat."""
    at = SystemClock().now() - timedelta(minutes=5)
    async with world.app.state.session_factory() as session:
        row = await insert_score_row(
            session,
            user_id=user.id if user else None,
            guest_session_id=guest.uuid if guest else None,
            base_cv_id=cv,
            job_posting_id=posting,
            pair=pair,
            scored_at=at,
        )
        await session.execute(
            text("UPDATE scoring_fit_score SET gaps = CAST(:g AS jsonb) WHERE id = :i"),
            {
                "g": f'[{{"gap": "{_MARKER}", "posting_quote": "{_MARKER}", "kind": "missing"}}]',
                "i": row,
            },
        )
        await session.commit()
    return row


async def _exists(world: World, row: UUID) -> bool:
    return await world.count("SELECT count(*) FROM scoring_fit_score WHERE id = :i", i=row) == 1


# --- AC-37: deleting a saved CV ----------------------------------------------------------------


async def test_ac37_the_accounts_own_delete_of_a_saved_cv_deletes_its_scores(world: World) -> None:
    user = await world.account()
    doomed_entry = await world.entry(user.owner)
    kept_entry = await world.entry(user.owner)
    doomed = await _score(world, user=user, cv=doomed_entry.cv_id.value, pair=1)
    kept = await _score(world, user=user, cv=kept_entry.cv_id.value, pair=2)

    response = await user.client.delete(
        f"{ME_BASE_CVS}/{doomed_entry.cv_id.value}", headers=user.headers
    )

    assert response.status_code == 204, response.text
    assert not await _exists(world, doomed), "a score survived its deleted CV"
    assert await _exists(world, kept)


async def test_ac37_an_admins_delete_of_a_users_saved_cv_deletes_its_scores(world: World) -> None:
    admin = await world.account(admin=True)
    user = await world.account()
    entry = await world.entry(user.owner)
    other = await world.entry(user.owner)
    doomed = await _score(world, user=user, cv=entry.cv_id.value, pair=1)
    kept = await _score(world, user=user, cv=other.cv_id.value, pair=2)

    response = await world.http.delete(
        f"{ADMIN_USERS}/{user.id}/base-cvs/{entry.cv_id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    assert not await _exists(world, doomed), "a score survived its CV deleted from /admin"
    assert await _exists(world, kept)


# --- AC-38: deleting a history entry ------------------------------------------------------------


async def test_ac38_the_accounts_own_delete_of_an_entry_deletes_the_scores_over_its_posting(
    world: World,
) -> None:
    user = await world.account()
    entry = await world.entry(user.owner)
    other = await world.entry(user.owner)
    doomed = await _score(world, user=user, posting=entry.posting_id.value, pair=1)
    kept = await _score(world, user=user, posting=other.posting_id.value, pair=2)

    response = await user.client.delete(f"{ME_RUNS}/{entry.run_id.value}", headers=user.headers)

    assert response.status_code == 204, response.text
    assert not await _exists(world, doomed), "a score survived its deleted posting"
    assert await _exists(world, kept)


async def test_ac38_an_admins_delete_of_an_entry_deletes_the_scores_over_its_posting(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    user = await world.account()
    entry = await world.entry(user.owner)
    doomed = await _score(world, user=user, posting=entry.posting_id.value, pair=1)

    response = await world.http.delete(
        f"{ADMIN_USERS}/{user.id}/tailoring-runs/{entry.run_id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    assert not await _exists(world, doomed), "a score survived its posting deleted from /admin"


# --- AC-39: the admin reads ---------------------------------------------------------------------


async def test_ac39_the_guest_list_and_overview_show_a_fit_score_count(world: World) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    for pair in (1, 2, 3):
        await _score(world, guest=guest, pair=pair)

    listed = await world.http.get(GUESTS, params={"limit": 50}, headers=admin.headers)
    overview = await world.http.get(f"{GUESTS}/{guest.uuid}", headers=admin.headers)

    assert listed.status_code == 200, listed.text
    assert overview.status_code == 200, overview.text
    (item,) = [i for i in listed.json()["items"] if i["id"] == str(guest.uuid)]
    assert item.get("fit_scores") == 3
    assert overview.json()["session"].get("fit_scores") == 3


async def test_ac39_a_guest_with_no_scores_shows_zero(world: World) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    other = await world.guest()
    await _score(world, guest=other)

    overview = await world.http.get(f"{GUESTS}/{guest.uuid}", headers=admin.headers)

    assert overview.status_code == 200, overview.text
    assert overview.json()["session"].get("fit_scores") == 0


async def test_ac39_the_user_detail_shows_a_fit_score_count_in_counts(world: World) -> None:
    admin = await world.account(admin=True)
    user = await world.account()
    other = await world.account()
    for pair in (1, 2):
        await _score(world, user=user, pair=pair)
    await _score(world, user=other, pair=1)

    response = await world.http.get(f"{ADMIN_USERS}/{user.id}", headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["counts"].get("fit_scores") == 2


async def test_ac39_no_score_or_gap_text_reaches_any_admin_body(world: World) -> None:
    """The marker sits in the gaps of a guest's score and of a user's. Every admin read that shows
    either owner (the list, the overview, the user detail and the user's content lists) is searched for
    it, and none may carry it. A count and nothing else (OQ-5)."""
    admin = await world.account(admin=True)
    guest = await world.guest()
    user = await world.account()
    guest_entry = await world.entry(guest.owner)
    user_entry = await world.entry(user.owner)
    await _score(
        world, guest=guest, cv=guest_entry.cv_id.value, posting=guest_entry.posting_id.value
    )
    await _score(world, user=user, cv=user_entry.cv_id.value, posting=user_entry.posting_id.value)
    urls = [
        GUESTS,
        f"{GUESTS}/{guest.uuid}",
        f"{GUESTS}/{guest.uuid}/base-cvs/{guest_entry.cv_id.value}",
        f"{GUESTS}/{guest.uuid}/job-postings/{guest_entry.posting_id.value}",
        f"{GUESTS}/{guest.uuid}/tailoring-runs/{guest_entry.run_id.value}",
        f"{ADMIN_USERS}/{user.id}",
        f"{ADMIN_USERS}/{user.id}/base-cvs",
        f"{ADMIN_USERS}/{user.id}/base-cvs/{user_entry.cv_id.value}",
        f"{ADMIN_USERS}/{user.id}/job-postings",
        f"{ADMIN_USERS}/{user.id}/job-postings/{user_entry.posting_id.value}",
        f"{ADMIN_USERS}/{user.id}/tailoring-runs",
        f"{ADMIN_USERS}/{user.id}/tailoring-runs/{user_entry.run_id.value}",
    ]

    for url in urls:
        params = {"limit": 50} if url == GUESTS else None
        response = await world.http.get(url, params=params, headers=admin.headers)
        assert response.status_code == 200, f"{url}: {response.text}"
        assert _MARKER not in response.text, f"{url} carried a score's gap text"
        assert "gaps" not in response.text, f"{url} carried a gaps field"
