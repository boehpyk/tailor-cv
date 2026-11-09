# ruff: noqa: F811
"""The admin demand counters and the account detail's credit counts (slice 5.2, T28, **red first**):
technical plan §4 rows 7-8; AC-29, the server half of AC-32; P-5.

    GET /api/admin/demand-counters?from=&to=        GET /api/admin/users/{id}  (+ credits_left, credit_entries)

Written from the acceptance criteria, not from the handler (T27's body is `NotImplementedError`).

Counter rows are **committed** on `tailorcraft_test` (the app is a concurrent one, a real session per
request), on days in January 2020 that no other test writes, and deleted in teardown. The ranges asked
for are on those days, so an unrelated counter another test committed today cannot move an answer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api.credits_support import ADMIN_COUNTERS, COUNTER_NAMES, reset_ledger
from tests.api.me_support import error_code
from tests.api.test_admin_users import (  # noqa: F401
    USERS,
    MakeAccount,
    _comparable,
    _no_store,
    _reset_redis_between_tests,
    http,
    make_account,
    seeded_ids,
    world_app,
)

D1 = date(2020, 1, 1)
UNMATCHED = "/api/admin/no-such-route"


@pytest_asyncio.fixture
async def counted(engine: AsyncEngine) -> AsyncIterator[None]:
    """Counts on 2020-01-01 (a few of each kind), 2020-01-03 (two kinds) and, outside every range the
    tests ask for, 2020-01-09."""
    rows = [
        (D1, "fit_scores", 7),
        (D1, "sign_ups", 2),
        (D1, "pack_clicks", 3),
        (D1, "pack_clickers", 1),
        (date(2020, 1, 3), "runs_succeeded", 5),
        (date(2020, 1, 3), "runs_failed", 1),
        (date(2020, 1, 9), "fit_scores", 99),
    ]
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM demand_daily_counter WHERE day < '2020-02-01'"))
        for day, counter, count in rows:
            await conn.execute(
                text("INSERT INTO demand_daily_counter (day, counter, count) VALUES (:d, :c, :n)"),
                {"d": day, "c": counter, "n": count},
            )
    yield
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM demand_daily_counter WHERE day < '2020-02-01'"))


def zeros() -> dict[str, int]:
    return dict.fromkeys(COUNTER_NAMES, 0)


def counts(**given: int) -> dict[str, int]:
    return {**zeros(), **given}


async def read(http: AsyncClient, admin: Any, frm: object, to: object) -> Response:
    return await http.get(
        ADMIN_COUNTERS, params={"from": str(frm), "to": str(to)}, headers=admin.headers
    )


# ------------------------------------------------------------------ AC-29: the range, zeros filled


async def test_ac29_every_day_in_the_range_is_listed_oldest_first_with_zeros_filled_and_totals(
    http: AsyncClient, make_account: MakeAccount, counted: None
) -> None:
    admin = await make_account(admin=True)

    response = await read(http, admin, "2020-01-01", "2020-01-04")

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == {"from", "to", "days", "totals"}
    assert (body["from"], body["to"]) == ("2020-01-01", "2020-01-04")
    assert [d["day"] for d in body["days"]] == [
        "2020-01-01",
        "2020-01-02",
        "2020-01-03",
        "2020-01-04",
    ]
    assert all(set(d) == {"day", "counts"} for d in body["days"])
    assert [d["counts"] for d in body["days"]] == [
        counts(fit_scores=7, sign_ups=2, pack_clicks=3, pack_clickers=1),
        zeros(),
        counts(runs_succeeded=5, runs_failed=1),
        zeros(),
    ]
    assert body["totals"] == counts(
        fit_scores=7,
        sign_ups=2,
        pack_clicks=3,
        pack_clickers=1,
        runs_succeeded=5,
        runs_failed=1,
    )


async def test_ac29_a_range_with_nothing_counted_is_all_zeros_not_empty(
    http: AsyncClient, make_account: MakeAccount, counted: None
) -> None:
    admin = await make_account(admin=True)

    response = await read(http, admin, "2020-01-05", "2020-01-06")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [d["counts"] for d in body["days"]] == [zeros(), zeros()]
    assert body["totals"] == zeros()


async def test_ac29_one_day_is_a_range_and_counts_nothing_outside_it(
    http: AsyncClient, make_account: MakeAccount, counted: None
) -> None:
    admin = await make_account(admin=True)

    response = await read(http, admin, "2020-01-03", "2020-01-03")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [d["day"] for d in body["days"]] == ["2020-01-03"]
    assert body["totals"] == counts(runs_succeeded=5, runs_failed=1)


async def test_ac29_ninety_two_days_inclusive_is_accepted_and_ninety_three_is_422(
    http: AsyncClient, make_account: MakeAccount, counted: None
) -> None:
    admin = await make_account(admin=True)

    accepted = await read(http, admin, D1, D1 + timedelta(days=91))
    refused = await read(http, admin, D1, D1 + timedelta(days=92))

    assert accepted.status_code == 200, accepted.text
    assert len(accepted.json()["days"]) == 92
    assert refused.status_code == 422, refused.text
    assert error_code(refused) == "invalid_counter_range"


async def test_p5_a_range_that_runs_backwards_is_422_invalid_counter_range(
    http: AsyncClient, make_account: MakeAccount, counted: None
) -> None:
    admin = await make_account(admin=True)

    response = await read(http, admin, "2020-01-04", "2020-01-03")

    assert response.status_code == 422, response.text
    assert error_code(response) == "invalid_counter_range"
    _no_store(response)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"from": "2020-01-01"},
        {"to": "2020-01-01"},
        {"from": "yesterday", "to": "2020-01-01"},
        {"from": "2020-01-01", "to": "2020-13-45"},
        {"from": "2020-01-01", "to": ""},
    ],
    ids=["none", "no-to", "no-from", "not-a-date", "impossible-date", "empty"],
)
async def test_p5_a_missing_or_malformed_date_is_422_validation_error(
    http: AsyncClient, make_account: MakeAccount, params: dict[str, str]
) -> None:
    admin = await make_account(admin=True)

    response = await http.get(ADMIN_COUNTERS, params=params, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


async def test_the_response_holds_counts_and_no_identity(
    http: AsyncClient, make_account: MakeAccount, counted: None
) -> None:
    admin = await make_account(admin=True)

    response = await read(http, admin, "2020-01-01", "2020-01-04")

    assert response.status_code == 200, response.text
    assert str(admin.id) not in response.text
    assert "@" not in response.text


# ------------------------------------------------------------------ AC-29: the admin firewall


async def test_ac29_a_plain_user_gets_the_404_of_an_unmatched_path_even_with_no_query(
    http: AsyncClient, make_account: MakeAccount, counted: None
) -> None:
    user = await make_account()

    unmatched = await http.get(UNMATCHED, headers=user.headers)
    with_range = await read(http, user, "2020-01-01", "2020-01-04")
    bare = await http.get(ADMIN_COUNTERS, headers=user.headers)

    assert unmatched.status_code == 404
    for refused in (with_range, bare):
        assert refused.status_code == 404, refused.text
        assert refused.content == unmatched.content
        assert _comparable(refused) == _comparable(unmatched)


async def test_ac29_anonymous_is_401_invalid_access_token(http: AsyncClient) -> None:
    response = await http.get(ADMIN_COUNTERS, params={"from": "2020-01-01", "to": "2020-01-02"})

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"


# ------------------------------------------------------- AC-32 (server half): the detail's counts


async def test_ac32_the_account_detail_counts_the_credits_left_and_the_entries(
    http: AsyncClient, make_account: MakeAccount, engine: AsyncEngine
) -> None:
    admin = await make_account(admin=True)
    target = await make_account()
    async with engine.begin() as conn:
        # 1 opening + 4 granted - 2 charged + 1 refunded = 4 left; entries: opening, grant, 2, 1 = 5
        await reset_ledger(conn, target.id, extra=4, charged=2, refunded=1)

    response = await http.get(f"{USERS}/{target.id}", headers=admin.headers)

    assert response.status_code == 200, response.text
    counts_ = response.json()["counts"]
    assert counts_["credits_left"] == 4
    assert counts_["credit_entries"] == 5


async def test_ac32_an_account_at_zero_reads_zero_credits_left_and_keeps_its_entries(
    http: AsyncClient, make_account: MakeAccount, engine: AsyncEngine
) -> None:
    admin = await make_account(admin=True)
    target = await make_account()
    async with engine.begin() as conn:
        await reset_ledger(conn, target.id, charged=1)

    response = await http.get(f"{USERS}/{target.id}", headers=admin.headers)

    assert response.status_code == 200, response.text
    assert (
        response.json()["counts"]["credits_left"],
        response.json()["counts"]["credit_entries"],
    ) == (
        0,
        2,
    )


async def test_ac32_another_accounts_ledger_does_not_leak_into_the_count(
    http: AsyncClient, make_account: MakeAccount, engine: AsyncEngine
) -> None:
    admin = await make_account(admin=True)
    target = await make_account()
    other = await make_account()
    async with engine.begin() as conn:
        await reset_ledger(conn, target.id)
        await reset_ledger(conn, other.id, extra=80)

    response = await http.get(f"{USERS}/{target.id}", headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["counts"]["credits_left"] == 1
