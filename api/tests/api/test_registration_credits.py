# ruff: noqa: F811
"""Confirming a registration opens the account, in one transaction (slice 5.2, T28): AC-1, AC-27
`sign_ups`, and the held spec "confirming commits the user, the welcome entry and the sign-up count
together" (T13/T21/T22).

Both halves go through the real confirm route on the real adapters; the mail is read the way a person
reads it (`_registered_and_mailed`), never by reaching into the database for a token.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.infrastructure.persistence.demand.daily_counter import SqlAlchemyDailyCounter
from tailorcraft.infrastructure.settings import Settings
from tests.api.credits_support import ME_CREDITS, counter_total
from tests.api.me_support import A_PASSWORD, bearer
from tests.api.test_auth_email_verification import (  # noqa: F401
    _confirm,
    _login,
    _registered_and_mailed,
    _reset_redis_between_tests,
    _unique_email,
    client,
    queue,
)
from tests.integration.fakes import RecordingAccountMailQueue


async def _rows(session: AsyncSession, email: str) -> tuple[int, int, int]:
    """(users, accounts, entries) for this address."""
    sql = {
        "users": "SELECT count(*) FROM identity_user WHERE email = :e",
        "accounts": (
            "SELECT count(*) FROM billing_credit_account a JOIN identity_user u ON u.id = a.user_id "
            "WHERE u.email = :e"
        ),
        "entries": (
            "SELECT count(*) FROM billing_credit_entry c JOIN identity_user u ON u.id = c.user_id "
            "WHERE u.email = :e"
        ),
    }
    out = [
        int((await session.execute(text(q), {"e": email.lower()})).scalar_one())
        for q in sql.values()
    ]
    return out[0], out[1], out[2]


async def test_ac1_confirming_opens_one_welcome_entry_counts_the_sign_up_and_reads_balance_one(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("welcome")
    token = await _registered_and_mailed(client, settings, session, queue, email)
    sign_ups = await counter_total(session, "sign_ups")

    confirmed = await _confirm(client, settings, token)

    assert confirmed.status_code == 204, confirmed.text
    assert await _rows(session, email) == (1, 1, 1)
    entry = (
        await session.execute(
            text(
                "SELECT c.kind, c.credits, c.tailoring_run_id FROM billing_credit_entry c "
                "JOIN identity_user u ON u.id = c.user_id WHERE u.email = :e"
            ),
            {"e": email},
        )
    ).one()
    assert (entry.kind, entry.credits, entry.tailoring_run_id) == ("welcome", 1, None)
    assert await counter_total(session, "sign_ups") - sign_ups == 1
    login = await _login(client, settings, email, A_PASSWORD)
    assert login.status_code == 200, login.text
    credits = await client.get(ME_CREDITS, headers=bearer(login.json()["access_token"]))
    assert credits.status_code == 200, credits.text
    assert (credits.json()["balance"], credits.json()["notice"]) == (1, "first_credit")


async def test_a_confirm_whose_count_fails_commits_no_user_no_account_no_entry_and_no_count(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count is written after the account is opened and before the commit, so a failure there is
    the "roll back after `open_account`" case: nothing may survive, and the same link must work again
    once the database answers (the pending row survives the rollback, plan §0.6)."""
    email = _unique_email("rollback")
    token = await _registered_and_mailed(client, settings, session, queue, email)
    sign_ups = await counter_total(session, "sign_ups")
    original = SqlAlchemyDailyCounter.increment

    async def _fails(self: SqlAlchemyDailyCounter, *args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated failure of the sign-up count")

    monkeypatch.setattr(SqlAlchemyDailyCounter, "increment", _fails)

    refused = await _confirm(client, settings, token)

    assert refused.status_code == 503, refused.text
    assert await _rows(session, email) == (0, 0, 0)
    assert await counter_total(session, "sign_ups") == sign_ups
    pending = (
        await session.execute(
            text("SELECT count(*) FROM identity_pending_registration WHERE email = :e"),
            {"e": email},
        )
    ).scalar_one()
    assert pending == 1, "the link was not consumed"

    monkeypatch.setattr(SqlAlchemyDailyCounter, "increment", original)
    retried = await _confirm(client, settings, token)

    assert retried.status_code == 204, retried.text
    assert await _rows(session, email) == (1, 1, 1)
    assert await counter_total(session, "sign_ups") - sign_ups == 1
