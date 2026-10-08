"""AC-41 (T34 PROOF): the identity token sweep spares the living.

`tests/integration/persistence/test_expired_identity_tokens_adapter.py` (T23) proves the adapter's
statements on rows in a year-2001 sandbox. This is the **whole system beside real data**: one user
with a saved CV, a history entry (a run and an export, with its file) and two **live** logins — each
refreshed once, so each carries a retired hash that must survive — and, in the same tables, expired
rows of every kind and unexpired links that were really mailed and really work.

The sweep is the worker's own composition root (`_build_identity_token_sweep`, bound to the
committing wrapper), run at a fixed instant `as_of`; the expired rows are of two kinds, **one second
before `as_of` and exactly at `as_of`**. A row at exactly `expires_at` is already refused by every
code path (`is_expired` is `at >= expires_at`), so it is already the sweep's — which is the boundary
this test pins.

**Counting without trusting an empty table.** The test database may hold a committed expired row some
other test left behind, so the expected report is *what was expired before this test seeded anything,
plus what it seeded*, read from the tables at `as_of` — never an absolute number.

What must hold afterwards:

- exactly the expired rows of each kind are gone (by id), counted as the report says;
- the expired login's retired hashes went with it by cascade, the **live** logins' did not, and both
  live refresh cookies still refresh;
- the unexpired registration link still confirms (204) and the unexpired reset link still resets
  (204) — the proof that "unexpired" was not just "not selected" but not harmed;
- the user, the saved CV, the history entry and its export row are untouched.

**Mutations (T34), production restored byte-exact (`git diff --exit-code -- api/src` clean).** Both on
the delete's predicate in `infrastructure/persistence/retention/expired_identity_tokens.py`; each
turns this one test red and no other of this file's:

- `table.c.expires_at <= as_of` -> `table.c.expires_at < as_of` (the boundary, AC-41's named mutation):
  red on `the row expiring exactly at as_of is expired and must be swept` (`assert not True`).
- `<= as_of` -> `<= as_of + timedelta(seconds=1)` (sweeping a row not yet expired): red on the report
  (`pending_registrations: 3 != 2`, and the resets) — a row that is not expired went.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.value_objects import IdentityTokenSweepReport
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container
from tests.api.account_mail_support import (
    confirmation_token,
    deliver_password_reset,
    deliver_registration,
    install_recording_queue,
    reset_token,
)
from tests.api.me_support import A_PASSWORD, count_rows, seed_entry, seed_user_and_sign_in
from tests.integration.fakes import RecordingAccountMailQueue

LOGIN_URL = "/api/auth/login"
REFRESH_URL = "/api/auth/refresh"
REGISTER_URL = "/api/auth/register"
CONFIRM_URL = "/api/auth/registration/confirm"
RESET_URL = "/api/auth/password-reset"
RESET_CONFIRM_URL = "/api/auth/password-reset/confirm"
_PHC = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
_TABLES = ("identity_pending_registration", "identity_password_reset", "identity_login")


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The register and reset limiters live in Redis, which the rollback does not reach."""


@pytest.fixture
def queue(app: FastAPI) -> RecordingAccountMailQueue:
    return install_recording_queue(app)


def _origin(settings: Settings) -> dict[str, str]:
    return {"Origin": settings.public_base_url}


def _new_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="https://testserver"
    )


async def _expired_before_seeding(session: AsyncSession, as_of: datetime) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in _TABLES:
        out[table] = int(
            (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE expires_at <= :t"),  # noqa: S608 -- allow-listed
                    {"t": as_of},
                )
            ).scalar_one()
        )
    return out


async def _seed_pending(session: AsyncSession, expires_at: datetime) -> UUID:
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_pending_registration (id, email, password_hash, requested_at, "
            "expires_at) VALUES (:i, :e, :p, :t, :x)"
        ),
        {
            "i": row_id,
            "e": f"{uuid4().hex}@example.com",
            "p": _PHC,
            "t": expires_at - timedelta(hours=24),
            "x": expires_at,
        },
    )
    return row_id


async def _seed_reset(session: AsyncSession, expires_at: datetime) -> UUID:
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_password_reset (id, email, requested_at, expires_at) "
            "VALUES (:i, :e, :t, :x)"
        ),
        {
            "i": row_id,
            "e": f"{uuid4().hex}@example.com",
            "t": expires_at - timedelta(hours=1),
            "x": expires_at,
        },
    )
    return row_id


async def _seed_login_with_a_retired_hash(
    session: AsyncSession, user_id: UUID, expires_at: datetime
) -> UUID:
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_login (id, user_id, created_at, expires_at, generation, "
            "current_token_hash, rotated_at, version) VALUES (:i, :u, :t, :x, 2, :h, :r, 1)"
        ),
        {
            "i": row_id,
            "u": user_id,
            "t": expires_at - timedelta(days=30),
            "x": expires_at,
            "h": uuid4().hex + uuid4().hex,
            "r": expires_at - timedelta(days=1),
        },
    )
    await session.execute(
        text(
            "INSERT INTO identity_retired_refresh_token (token_hash, login_id, generation, "
            "retired_at) VALUES (:h, :l, 1, :t)"
        ),
        {"h": uuid4().hex + uuid4().hex, "l": row_id, "t": expires_at - timedelta(days=1)},
    )
    return row_id


async def _exists(session: AsyncSession, table: str, row_id: UUID) -> bool:
    assert table in _TABLES
    return bool(
        (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE id = :i"),  # noqa: S608 -- allow-listed
                {"i": row_id},
            )
        ).scalar_one()
    )


async def _retired_for(session: AsyncSession, login_ids: list[UUID]) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM identity_retired_refresh_token WHERE login_id = ANY(:ids)"
                ),
                {"ids": login_ids},
            )
        ).scalar_one()
    )


async def _live_login_ids(session: AsyncSession, user_id: UUID) -> list[UUID]:
    rows = await session.execute(
        text("SELECT id FROM identity_login WHERE user_id = :u"), {"u": user_id}
    )
    return [r[0] for r in rows]


async def test_the_sweep_deletes_exactly_the_expired_rows_beside_a_users_data_and_spares_everything_else(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    """See the module docstring: AC-41 whole, one test, because the claim is about one sweep."""
    as_of = SystemClock().now()
    before = await _expired_before_seeding(session, as_of)

    # --- the user and everything of theirs the sweep must not touch -------------------------
    email = f"t34-{uuid4().hex[:12]}@example.com"
    _, user_uuid = await seed_user_and_sign_in(client, settings, email=email)
    second_browser = _new_client(app)
    logged_in = await second_browser.post(
        LOGIN_URL, json={"email": email, "password": A_PASSWORD}, headers=_origin(settings)
    )
    assert logged_in.status_code == 200, logged_in.text
    for browser in (client, second_browser):  # a refresh each: retires one hash per live login
        refreshed = await browser.post(REFRESH_URL, headers=_origin(settings))
        assert refreshed.status_code == 200, refreshed.text
    entry = await seed_entry(session, settings, UserOwner(UserId(user_uuid)), at=as_of)
    live_logins = await _live_login_ids(session, user_uuid)
    assert len(live_logins) == 2
    retired_on_live = await _retired_for(session, live_logins)
    assert retired_on_live >= 2, "each refresh retires a hash; the survival check needs some"
    user_data = {
        "identity_user": await count_rows(session, "identity_user", id=user_uuid),
        "intake_base_cv": await count_rows(session, "intake_base_cv", user_id=user_uuid),
        "tailoring_run": await count_rows(session, "tailoring_run", user_id=user_uuid),
        "export_job": await count_rows(session, "export_job", user_id=user_uuid),
    }
    assert user_data == {
        "identity_user": 1,
        "intake_base_cv": 1,
        "tailoring_run": 1,
        "export_job": 1,
    }, entry

    # --- unexpired links, really mailed ------------------------------------------------------
    pending_email = f"t34-new-{uuid4().hex[:12]}@example.com"
    registered = await client.post(
        REGISTER_URL,
        json={"email": pending_email, "password": A_PASSWORD},
        headers=_origin(settings),
    )
    assert registered.status_code == 202, registered.text
    confirm_mailer = await deliver_registration(settings, session, queue.registrations[-1])
    confirm_link = confirmation_token(confirm_mailer)
    reset_requested = await client.post(RESET_URL, json={"email": email}, headers=_origin(settings))
    assert reset_requested.status_code == 202, reset_requested.text
    reset_mailer = await deliver_password_reset(settings, session, queue.resets[-1])
    reset_link = reset_token(reset_mailer)

    # --- expired rows of each kind: one second before as_of, and exactly at as_of ------------
    expired_pending = [
        await _seed_pending(session, as_of - timedelta(seconds=1)),
        await _seed_pending(session, as_of),
    ]
    expired_resets = [
        await _seed_reset(session, as_of - timedelta(seconds=1)),
        await _seed_reset(session, as_of),
    ]
    expired_logins = [
        await _seed_login_with_a_retired_hash(session, user_uuid, as_of - timedelta(seconds=1)),
        await _seed_login_with_a_retired_hash(session, user_uuid, as_of),
    ]
    assert await _retired_for(session, expired_logins) == 2
    unexpired_pending = await _seed_pending(session, as_of + timedelta(seconds=1))
    unexpired_reset = await _seed_reset(session, as_of + timedelta(seconds=1))
    await session.commit()

    # --- the sweep ---------------------------------------------------------------------------
    report = await container._build_identity_token_sweep(session)(as_of)

    for row_id in expired_pending:
        assert not await _exists(session, "identity_pending_registration", row_id), (
            "the row expiring exactly at as_of is expired and must be swept"
        )
    for row_id in expired_resets:
        assert not await _exists(session, "identity_password_reset", row_id), (
            "the row expiring exactly at as_of is expired and must be swept"
        )
    for row_id in expired_logins:
        assert not await _exists(session, "identity_login", row_id), (
            "the row expiring exactly at as_of is expired and must be swept"
        )
    assert report == IdentityTokenSweepReport(
        pending_registrations=before["identity_pending_registration"] + 2,
        password_resets=before["identity_password_reset"] + 2,
        logins=before["identity_login"] + 2,
    )
    assert await _retired_for(session, expired_logins) == 0  # the cascade
    assert await _exists(session, "identity_pending_registration", unexpired_pending)
    assert await _exists(session, "identity_password_reset", unexpired_reset)
    assert sorted(await _live_login_ids(session, user_uuid)) == sorted(live_logins)
    assert await _retired_for(session, live_logins) == retired_on_live
    assert {
        "identity_user": await count_rows(session, "identity_user", id=user_uuid),
        "intake_base_cv": await count_rows(session, "intake_base_cv", user_id=user_uuid),
        "tailoring_run": await count_rows(session, "tailoring_run", user_id=user_uuid),
        "export_job": await count_rows(session, "export_job", user_id=user_uuid),
    } == user_data

    # --- the living still live ---------------------------------------------------------------
    for browser in (client, second_browser):
        refreshed = await browser.post(REFRESH_URL, headers=_origin(settings))
        assert refreshed.status_code == 200, refreshed.text
    confirmed = await client.post(
        CONFIRM_URL, json={"token": confirm_link}, headers=_origin(settings)
    )
    assert confirmed.status_code == 204, confirmed.text
    reset: Any = await client.post(
        RESET_CONFIRM_URL,
        json={"token": reset_link, "password": "an entirely different passphrase 7"},
        headers=_origin(settings),
    )
    assert reset.status_code == 204, reset.text
    await second_browser.aclose()
