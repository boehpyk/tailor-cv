"""The HTTP contract of credits, pack clicks and the payment notice (slice 5.2, T28, **red first**):
technical plan §4 rows 1-3; AC-4, AC-5, AC-25, AC-26; F-18, F-21, P-1, P-2.

    GET  /api/me/credits           PUT /api/me/payment-notice {opted_in}
    POST /api/me/pack-clicks {pack}

Written from the acceptance criteria and the failure contract, not from the handlers (T27's bodies are
`NotImplementedError`, so a red here reads `assert 500 == 200`).

- **Arrangement is SQL** (`credits_support.reset_ledger`): the response's arithmetic is tested against
  entries the test wrote, not against an aggregate the test then trusts.
- **Bearer only.** A `__Host-tc_guest` cookie is not a credential on any of these routes.
- **A counter is read as a delta** (`counter_total` before and after): the table is global and other
  tests commit real rows into it.
- **Every response a handler produces is `no-store`** (plan §4); a credential 401 and FastAPI's own 422
  are produced before a handler runs, and `no-store` on them is not in §4's text.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from tailorcraft.infrastructure.settings import Settings
from tests.api.account_mail_support import install_recording_queue
from tests.api.credits_support import (
    ME_CREDITS,
    ME_PACK_CLICKS,
    ME_PAYMENT_NOTICE,
    all_counter_totals,
    counter_total,
    reset_ledger,
)
from tests.api.me_support import Account, error_code, mint_guest, new_client, register

PACKS = [
    {"id": "pack_3", "credits": 3, "price_cents": 500},
    {"id": "pack_10", "credits": 10, "price_cents": 1200},
]
CREDITS_KEYS = {
    "balance",
    "out_of_credits",
    "notice",
    "payment_notice_opted_in",
    "packs",
    "entries",
}
ENTRY_KEYS = {"kind", "credits", "tailoring_run_id", "at"}


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Nothing here is limited, but the bearer path shares Redis with the suite."""


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


async def account_with(
    client: AsyncClient, settings: Settings, session: AsyncSession, **ledger: object
) -> Account:
    account = await register(client, settings)
    await reset_ledger(session, account.user_id.value, **ledger)  # type: ignore[arg-type]
    await session.commit()
    return account


async def credits_of(client: AsyncClient, account: Account) -> dict[str, Any]:
    """`GET /api/me/credits` as a precondition of an assertion: a 500 here must read as a status."""
    response = await client.get(ME_CREDITS, headers=account.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def no_store(response: Response) -> None:
    assert response.headers.get("cache-control") == "no-store", dict(response.headers)


# ------------------------------------------------------------------------------ GET /credits


async def test_the_balance_is_the_sum_of_the_entries_and_the_response_has_exactly_its_keys(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session, extra=4, charged=3, refunded=1)

    response = await client.get(ME_CREDITS, headers=account.headers)

    assert response.status_code == 200, response.text
    no_store(response)
    body = response.json()
    assert set(body) == CREDITS_KEYS
    assert body["balance"] == 1 + 4 - 3 + 1
    assert body["out_of_credits"] is False
    assert body["payment_notice_opted_in"] is False
    assert body["packs"] == PACKS


async def test_a_new_welcome_account_reads_balance_one_with_the_first_credit_notice(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session, opening="welcome")

    body = await credits_of(client, account)

    assert (body["balance"], body["notice"], body["out_of_credits"]) == (1, "first_credit", False)


@pytest.mark.parametrize(
    ("ledger", "notice"),
    [
        ({"opening": "existing_account"}, "credits_introduced"),
        ({"opening": "welcome"}, "first_credit"),
        ({"opening": "existing_account", "extra": 5}, "credits_introduced"),
        ({"opening": "welcome", "charged": 1}, None),
        ({"opening": "existing_account", "charged": 1}, None),
        ({"opening": "welcome", "extra": 1, "charged": 1, "refunded": 1}, None),
    ],
    ids=[
        "existing-never-charged",
        "welcome-never-charged",
        "existing-with-a-grant",
        "welcome-charged",
        "existing-charged",
        "charged-then-refunded-is-still-charged",
    ],
)
async def test_ac5_the_notice_follows_the_opening_kind_until_anything_is_charged(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    ledger: dict[str, object],
    notice: str | None,
) -> None:
    account = await account_with(client, settings, session, **ledger)

    body = await credits_of(client, account)

    assert body["notice"] == notice


async def test_at_balance_zero_the_response_says_out_of_credits(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session, charged=1)

    body = await credits_of(client, account)

    assert (body["balance"], body["out_of_credits"]) == (0, True)


async def test_the_entries_are_newest_first_and_carry_the_run_only_on_a_charge_or_refund(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session, extra=2, charged=2, refunded=1)

    entries = (await credits_of(client, account))["entries"]

    # oldest first as written: welcome, grant, charge, charge, refund
    assert [e["kind"] for e in entries] == ["refund", "charge", "charge", "grant", "welcome"]
    assert [e["credits"] for e in entries] == [1, -1, -1, 2, 1]
    assert all(set(e) == ENTRY_KEYS for e in entries)
    assert [e["tailoring_run_id"] is not None for e in entries] == [True, True, True, False, False]
    assert entries[0]["tailoring_run_id"] == entries[2]["tailoring_run_id"]  # the refund names it
    instants = [datetime.fromisoformat(e["at"]) for e in entries]
    assert instants == sorted(instants, reverse=True)


async def test_ac4_a_history_of_a_hundred_and_twenty_entries_answers_the_newest_hundred(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session, extra_rows=119)

    body = await credits_of(client, account)

    assert body["balance"] == 120  # the balance counts every entry, not just the ones shown
    assert len(body["entries"]) == 100
    kinds = [e["kind"] for e in body["entries"]]
    assert "welcome" not in kinds, "the oldest entry is the one cut"


async def test_one_account_never_reads_another_accounts_ledger(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    rich = await account_with(client, settings, session, extra=50)
    poor = await account_with(client, settings, session, charged=1)

    rich_body = await credits_of(client, rich)
    poor_body = await credits_of(client, poor)

    assert (rich_body["balance"], poor_body["balance"]) == (51, 0)


# ---------------------------------------------------------------------- credentials, P-2, F-21


ROUTES = [
    ("GET", ME_CREDITS, None),
    ("POST", ME_PACK_CLICKS, {"pack": "pack_3"}),
    ("PUT", ME_PAYMENT_NOTICE, {"opted_in": True}),
]


@pytest.mark.parametrize(("method", "url", "body"), ROUTES, ids=["credits", "click", "notice"])
async def test_p2_no_bearer_is_401_and_a_guest_cookie_is_not_a_credential(
    client: AsyncClient,
    session: AsyncSession,
    method: str,
    url: str,
    body: dict[str, object] | None,
) -> None:
    bare = await client.request(method, url, json=body)
    await mint_guest(client, session)  # the cookie is now on `client`
    with_cookie = await client.request(method, url, json=body)

    for response in (bare, with_cookie):
        assert response.status_code == 401, response.text
        assert error_code(response) == "invalid_access_token"
        assert "set-cookie" not in response.headers


@pytest.mark.parametrize(("method", "url", "body"), ROUTES, ids=["credits", "click", "notice"])
async def test_f21_a_disabled_account_is_403_account_disabled(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    method: str,
    url: str,
    body: dict[str, object] | None,
) -> None:
    account = await register(client, settings)
    await session.execute(
        text("UPDATE identity_user SET disabled_at = now() WHERE id = :u"),
        {"u": account.user_id.value},
    )
    await session.commit()

    response = await client.request(method, url, json=body, headers=account.headers)

    assert response.status_code == 403, response.text
    assert error_code(response) == "account_disabled"


@pytest.mark.parametrize(("method", "url", "body"), ROUTES, ids=["credits", "click", "notice"])
async def test_f18_the_database_down_is_503_service_unavailable(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    url: str,
    body: dict[str, object] | None,
) -> None:
    account = await register(client, settings)
    real_execute = session.execute

    async def _billing_fails(statement: Any, *args: Any, **kwargs: Any) -> Any:
        # Only the billing tables: a failure at authentication would pass without reaching a handler.
        if "billing_credit" in str(statement):
            raise SQLAlchemyError("simulated outage (F-18)")
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", _billing_fails)

    response = await client.request(method, url, json=body, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"


async def test_no_credit_account_is_a_named_500_with_no_store_logging_the_user_id_only(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    """Eager opening makes this unreachable in practice (AC-2); if it happens, it is findable."""
    account = await register(client, settings)
    await session.execute(
        text("DELETE FROM billing_credit_account WHERE user_id = :u"), {"u": account.user_id.value}
    )
    await session.commit()

    with capture_logs() as logs:
        response = await client.get(ME_CREDITS, headers=account.headers)

    assert response.status_code == 500, response.text
    assert error_code(response) == "credit_account_missing"
    no_store(response)
    missing = [line for line in logs if line["event"] == "credits.account_missing"]
    assert missing == [
        {
            "event": "credits.account_missing",
            "user_id": str(account.user_id.value),
            "log_level": "error",
        }
    ]


# ------------------------------------------------------------------ POST /pack-clicks (AC-25)


@pytest.mark.parametrize("pack", ["pack_3", "pack_10"])
async def test_ac25_a_click_is_200_charges_nothing_and_counts_one_click_and_one_clicker(
    client: AsyncClient, settings: Settings, session: AsyncSession, pack: str
) -> None:
    account = await account_with(client, settings, session, extra=2)
    before = await all_counter_totals(session)

    response = await client.post(ME_PACK_CLICKS, json={"pack": pack}, headers=account.headers)

    assert response.status_code == 200, response.text
    no_store(response)
    assert response.json() == {"payment_notice_opted_in": False}
    after = await all_counter_totals(session)
    assert after["pack_clicks"] - before["pack_clicks"] == 1
    assert after["pack_clickers"] - before["pack_clickers"] == 1
    assert {
        k: v - before[k] for k, v in after.items() if k not in ("pack_clicks", "pack_clickers")
    } == {k: 0 for k in after if k not in ("pack_clicks", "pack_clickers")}
    balance = (await credits_of(client, account))["balance"]
    assert balance == 3, "a click sells nothing and charges nothing"


async def test_ac25_a_second_click_counts_the_click_but_not_the_clicker_again(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session)
    before = await all_counter_totals(session)

    first = await client.post(ME_PACK_CLICKS, json={"pack": "pack_3"}, headers=account.headers)
    second = await client.post(ME_PACK_CLICKS, json={"pack": "pack_10"}, headers=account.headers)

    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    after = await all_counter_totals(session)
    assert after["pack_clicks"] - before["pack_clicks"] == 2
    assert after["pack_clickers"] - before["pack_clickers"] == 1


async def test_two_accounts_are_two_clickers(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    one = await account_with(client, settings, session)
    two = await account_with(client, settings, session)
    before = await counter_total(session, "pack_clickers")

    for who in (one, two):
        await client.post(ME_PACK_CLICKS, json={"pack": "pack_3"}, headers=who.headers)

    assert await counter_total(session, "pack_clickers") - before == 2


async def test_a_click_answers_the_accounts_opt_in_state(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session)
    await client.put(ME_PAYMENT_NOTICE, json={"opted_in": True}, headers=account.headers)

    response = await client.post(ME_PACK_CLICKS, json={"pack": "pack_3"}, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"payment_notice_opted_in": True}


@pytest.mark.parametrize(
    "body",
    [
        {"pack": "pack_99"},
        {"pack": ""},
        {"pack": None},
        {"pack": 3},
        {},
        {"pack": "pack_3", "price_cents": 1},
        [],
    ],
    ids=["unknown", "empty", "null", "number", "missing", "stray-field", "not-an-object"],
)
async def test_p1_an_unknown_pack_or_a_malformed_body_is_422_and_counts_nothing(
    client: AsyncClient, settings: Settings, session: AsyncSession, body: object
) -> None:
    account = await account_with(client, settings, session)
    before = await all_counter_totals(session)

    response = await client.post(ME_PACK_CLICKS, json=body, headers=account.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    assert await all_counter_totals(session) == before


# --------------------------------------------------------- PUT /payment-notice (AC-26)


async def test_ac26_opting_in_then_out_is_recorded_and_read_back_by_the_credits_route(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session)

    on = await client.put(ME_PAYMENT_NOTICE, json={"opted_in": True}, headers=account.headers)
    read_on = await credits_of(client, account)
    off = await client.put(ME_PAYMENT_NOTICE, json={"opted_in": False}, headers=account.headers)
    read_off = await credits_of(client, account)

    assert on.status_code == 200, on.text
    assert on.json() == {"payment_notice_opted_in": True}
    no_store(on)
    assert read_on["payment_notice_opted_in"] is True
    assert off.status_code == 200, off.text
    assert off.json() == {"payment_notice_opted_in": False}
    assert read_off["payment_notice_opted_in"] is False


@pytest.mark.parametrize("opted_in", [True, False])
async def test_ac26_the_same_choice_twice_is_the_same_answer(
    client: AsyncClient, settings: Settings, session: AsyncSession, opted_in: bool
) -> None:
    account = await account_with(client, settings, session)

    first = await client.put(
        ME_PAYMENT_NOTICE, json={"opted_in": opted_in}, headers=account.headers
    )
    second = await client.put(
        ME_PAYMENT_NOTICE, json={"opted_in": opted_in}, headers=account.headers
    )

    assert (first.status_code, second.status_code) == (200, 200)
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert first.json() == second.json() == {"payment_notice_opted_in": opted_in}


async def test_ac26_the_opt_in_instant_is_kept_from_the_first_yes(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session)
    sql = text("SELECT payment_notice_opted_in_at FROM billing_credit_account WHERE user_id = :u")

    await client.put(ME_PAYMENT_NOTICE, json={"opted_in": True}, headers=account.headers)
    first = (await session.execute(sql, {"u": account.user_id.value})).scalar_one()
    await client.put(ME_PAYMENT_NOTICE, json={"opted_in": True}, headers=account.headers)
    second = (await session.execute(sql, {"u": account.user_id.value})).scalar_one()

    assert first is not None
    assert second == first, "a repeated yes is a no-op, not a new consent instant"


@pytest.mark.parametrize(
    "body",
    [
        {"opted_in": "yes"},
        {"opted_in": "true"},
        {"opted_in": 1},
        {"opted_in": 0},
        {"opted_in": None},
        {},
        {"opted_in": True, "email": "a@b.c"},
        [],
    ],
    ids=["yes", "true-string", "one", "zero", "null", "missing", "stray-field", "not-an-object"],
)
async def test_the_choice_is_strict_a_guess_is_a_422_and_changes_nothing(
    client: AsyncClient, settings: Settings, session: AsyncSession, body: object
) -> None:
    account = await account_with(client, settings, session)

    response = await client.put(ME_PAYMENT_NOTICE, json=body, headers=account.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    read = await credits_of(client, account)
    assert read["payment_notice_opted_in"] is False


async def test_ac26_opting_in_queues_and_sends_no_mail(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession
) -> None:
    queue = install_recording_queue(app)
    account = await account_with(client, settings, session)

    put = await client.put(ME_PAYMENT_NOTICE, json={"opted_in": True}, headers=account.headers)
    click = await client.post(ME_PACK_CLICKS, json={"pack": "pack_3"}, headers=account.headers)

    assert (put.status_code, click.status_code) == (200, 200), (put.text, click.text)
    assert queue.registrations == []
    assert queue.resets == []


async def test_a_response_never_carries_an_email_address_or_another_account_id(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await account_with(client, settings, session)

    responses = [
        await client.put(ME_PAYMENT_NOTICE, json={"opted_in": True}, headers=account.headers),
        await client.get(ME_CREDITS, headers=account.headers),
        await client.post(ME_PACK_CLICKS, json={"pack": "pack_3"}, headers=account.headers),
    ]

    for response in responses:
        assert response.status_code == 200, response.text
        assert "@" not in response.text
        assert str(account.user_id.value) not in response.text
