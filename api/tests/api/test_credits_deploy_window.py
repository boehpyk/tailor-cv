"""AC-41 and F-20: the new API on the old schema (slice 5.2, T28).

During a deploy the new API briefly serves requests before the migration has run, so the billing
tables, the counter table and the draft-score column do not exist yet. The release is safe if every
handler that needs them **answers 503 `service_unavailable` and commits nothing**, so the same request
works seconds later. "Nothing" is read as rows: no run, no score, no click, no enqueue, no model call.

The old schema is made the honest way: the test's own transaction drops the three new tables and the
new column (DDL is transactional in PostgreSQL), so the real handlers run against the real database
without them, and the outer rollback puts them back. Nothing is mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.infrastructure.api.deps import get_tailoring_queue
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.settings import Settings
from tests.api.credits_support import (
    ME_CREDITS,
    ME_PACK_CLICKS,
    ME_PAYMENT_NOTICE,
    ME_RUNS,
    fit_scores_url,
)
from tests.api.me_support import Account, count_rows, error_code, new_client, register, seed_entry
from tests.integration.fakes import FakeFitScorer, FakeTailoringQueue
from tests.integration.scoring.support import assessed


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The tailoring and fit-score limiters live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest_asyncio.fixture
async def old_world(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> AsyncIterator[dict[str, Any]]:
    """An account with a succeeded run and its inputs, on a schema as the previous release left it."""
    queue = FakeTailoringQueue()
    app.dependency_overrides[get_tailoring_queue] = lambda: queue
    scorer = FakeFitScorer(assessed())
    app.state.fit_scorer = scorer
    account: Account = await register(client, settings)
    entry = await seed_entry(
        session,
        settings,
        account.owner,
        at=SystemClock().now() - timedelta(minutes=10),
        ready_formats=(),
    )
    ids = {
        "account": account,
        "entry": entry,
        "queue": queue,
        "scorer": scorer,
        "runs_before": await count_rows(session, "tailoring_run", user_id=account.user_id.value),
    }
    await session.execute(text("ALTER TABLE scoring_fit_score DROP COLUMN tailoring_run_id"))
    await session.execute(
        text(
            "DROP TABLE billing_credit_entry, billing_credit_account, demand_daily_counter CASCADE"
        )
    )
    await session.commit()
    yield ids


def unavailable(response: Any) -> None:
    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", ME_CREDITS, None),
        ("POST", ME_PACK_CLICKS, {"pack": "pack_3"}),
        ("PUT", ME_PAYMENT_NOTICE, {"opted_in": True}),
    ],
    ids=["credits", "pack-click", "payment-notice"],
)
async def test_ac41_a_billing_route_before_the_migration_is_503_and_the_same_request_would_retry(
    client: AsyncClient, old_world: dict[str, Any], method: str, url: str, body: Any
) -> None:
    account: Account = old_world["account"]

    unavailable(await client.request(method, url, json=body, headers=account.headers))


async def test_ac41_an_account_tailoring_request_before_the_migration_is_503_with_no_run_and_no_enqueue(
    client: AsyncClient, session: AsyncSession, old_world: dict[str, Any]
) -> None:
    account: Account = old_world["account"]
    entry = old_world["entry"]
    body = {
        "base_cv_id": str(entry.cv_id.value),
        "job_posting_id": str(entry.posting_id.value),
    }

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    unavailable(response)
    assert (
        await count_rows(session, "tailoring_run", user_id=account.user_id.value)
        == old_world["runs_before"]
    ), "the run inserted before the charge failed went with the transaction"
    assert old_world["queue"].enqueued == []


@pytest.mark.parametrize("side", ["before", "after"])
async def test_ac41_a_comparison_before_the_migration_is_503_with_no_model_call_and_no_row(
    client: AsyncClient, session: AsyncSession, old_world: dict[str, Any], side: str
) -> None:
    account: Account = old_world["account"]

    response = await client.post(
        fit_scores_url(old_world["entry"].run_id.value, side), headers=account.headers
    )

    unavailable(response)
    assert old_world["scorer"].calls == [], "no paid call is made for a store that cannot take it"
    assert await count_rows(session, "scoring_fit_score", user_id=account.user_id.value) == 0


async def test_ac41_the_comparison_read_before_the_migration_is_503(
    client: AsyncClient, old_world: dict[str, Any]
) -> None:
    account: Account = old_world["account"]

    unavailable(
        await client.get(fit_scores_url(old_world["entry"].run_id.value), headers=account.headers)
    )


async def test_ac41_the_existing_fit_score_check_before_the_migration_is_503_with_no_model_call(
    client: AsyncClient, session: AsyncSession, old_world: dict[str, Any]
) -> None:
    account: Account = old_world["account"]
    entry = old_world["entry"]

    response = await client.post(
        "/api/me/fit-scores",
        json={
            "base_cv_id": str(entry.cv_id.value),
            "job_posting_id": str(entry.posting_id.value),
        },
        headers=account.headers,
    )

    unavailable(response)
    assert old_world["scorer"].calls == []
    assert await count_rows(session, "scoring_fit_score", user_id=account.user_id.value) == 0
