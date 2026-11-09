"""The account `POST /api/me/tailoring-runs` as the credit gate, over HTTP (slice 5.2, T28, **red first**):
AC-6, AC-7, AC-8, AC-13; F-1, F-2, F-10, F-15, F-16, F-24.

Two groups. The **sequential** ones run on the shared `app` (one SAVEPOINT-bound session) and read the
rows the request left. The **races** run on a `World`: a concurrent app (a real session and connection
per request) over real, committed rows, deleted in the fixture's teardown.

**How each race is staged.** A race that "usually" overlaps is a test that usually passes, so each one
holds the competing requests at the moment the spec names, by wrapping one repository method and
delegating to the real one:

- F-2: `lock_for_charge` is the seam. Both requests have already passed the soft active-run check and
  inserted their `queued` row when they reach it, and a barrier holds each there until the other has
  arrived. The database then serializes them on the account row, and what the loser sees under the lock
  is what the hard re-check exists to refuse. The seam is "reaching the lock", so it survives any fix
  that keeps the lock.
- F-15: the charging request holds the account lock (the wrapper returns only when told to), a refund
  for a different charged run commits meanwhile from a second session, and only then is the charge let
  go. A refund takes no account lock (plan §0.4), so it must not wait for the charge.
- F-16: the user is deleted on another connection **inside `SqlAlchemyTailoringRunRepository.add`**,
  after authorization and before the INSERT, and the wrapper asserts that it really landed there.

Against the T27 skeleton the charge is already wired (T17), so the sequential tests that describe
today's behaviour are green on arrival; they are here as the HTTP half of the money rules.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.application.billing.refund_credit_for_run import (
    RefundCreditCommand,
    RefundCreditForRun,
)
from tailorcraft.domain.billing.value_objects import ChargedRunRef
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.errors import TailoringNotQueued
from tailorcraft.infrastructure.api.deps import get_tailoring_queue
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.repositories.billing.credit_account import (
    SqlAlchemyCreditAccountRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.credits_support import ME_RUNS, counter_total, reset_ledger
from tests.api.me_support import (
    Account,
    count_rows,
    error_body,
    error_code,
    new_client,
    register,
)
from tests.integration.fakes import FakeTailoringQueue
from tests.integration.owners import extracted_cv, extraction_failed_cv, pasted_posting, running_run

_STEP = 30.0


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The account tailoring limiter lives in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


async def inputs(session: AsyncSession, owner: Owner, *, extracted: bool = True) -> dict[str, str]:
    at = SystemClock().now() - timedelta(minutes=5)
    cv = (extracted_cv if extracted else extraction_failed_cv)(owner, at)
    posting = pasted_posting(owner, at)
    await SqlAlchemyBaseCvRepository(session).add(cv)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    body = {"base_cv_id": str(cv.id.value), "job_posting_id": str(posting.id.value)}
    await session.commit()
    return body


async def funded(
    client: AsyncClient, session: AsyncSession, settings: Settings, **ledger: Any
) -> tuple[Account, dict[str, str]]:
    account = await register(client, settings)
    await reset_ledger(session, account.user_id.value, **ledger)
    await session.commit()
    return account, await inputs(session, account.owner)


async def balance_of(session: AsyncSession, user_id: UUID) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT COALESCE(SUM(credits), 0) FROM billing_credit_entry WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).scalar_one()
    )


def queue_on(app: FastAPI, *, refuse: bool = False) -> FakeTailoringQueue:
    queue = FakeTailoringQueue(outcome=TailoringNotQueued("simulated refusal") if refuse else None)
    app.dependency_overrides[get_tailoring_queue] = lambda: queue
    return queue


# ------------------------------------------------------------------- AC-6: charge with the run


@pytest.mark.parametrize("balance", [1, 3])
async def test_ac6_a_request_with_credit_is_202_and_one_charge_naming_the_run_in_the_same_commit(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, balance: int
) -> None:
    queue = queue_on(app)
    account, body = await funded(client, session, settings, extra=balance - 1)

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert await balance_of(session, account.user_id.value) == balance - 1
    charges = (
        await session.execute(
            text(
                "SELECT credits, tailoring_run_id FROM billing_credit_entry "
                "WHERE user_id = :u AND kind = 'charge'"
            ),
            {"u": account.user_id.value},
        )
    ).all()
    assert [(c.credits, str(c.tailoring_run_id)) for c in charges] == [(-1, run_id)]
    assert [str(r.value) for r in queue.enqueued] == [run_id]


# ------------------------------------------------------------------- AC-7, F-1: balance 0


async def test_ac7_f1_at_balance_zero_the_request_is_409_no_credits_and_nothing_is_written(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    queue = queue_on(app)
    account, body = await funded(client, session, settings, charged=1)
    entries_before = await count_rows(
        session, "billing_credit_entry", user_id=account.user_id.value
    )

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "no_credits"
    assert response.headers.get("cache-control") == "no-store"
    assert "retry-after" not in {h.lower() for h in response.headers}, "nothing is held (ADR-0031)"
    assert "retry_not_before" not in error_body(response)
    assert await count_rows(session, "tailoring_run", user_id=account.user_id.value) == 0
    assert (
        await count_rows(session, "billing_credit_entry", user_id=account.user_id.value)
        == entries_before
    )
    assert queue.enqueued == []


async def test_ac7_the_request_that_spends_the_last_credit_works_and_the_next_is_refused(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    queue_on(app)
    account, body = await funded(client, session, settings)  # balance 1

    first = await client.post(ME_RUNS, json=body, headers=account.headers)
    await session.execute(
        text(
            "UPDATE tailoring_run SET status = 'failed', failure_reason = 'llm_error', "
            "completed_at = now() WHERE user_id = :u"
        ),
        {"u": account.user_id.value},
    )
    await session.commit()
    second = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert first.status_code == 202, first.text
    assert second.status_code == 409, second.text
    assert error_code(second) == "no_credits"


# ------------------------------------------------- refusals that precede the charge (F-24, H-16)


async def test_f24_a_cv_not_extracted_is_409_before_the_charge_and_the_balance_is_unchanged(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    queue_on(app)
    account, _ = await funded(client, session, settings)
    body = await inputs(session, account.owner, extracted=False)

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "base_cv_not_extracted"
    assert await balance_of(session, account.user_id.value) == 1


async def test_an_active_run_is_refused_before_the_charge_and_names_it(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    queue_on(app)
    account, body = await funded(client, session, settings, extra=2)
    active = running_run(
        account.owner,
        SystemClock().now() - timedelta(minutes=1),
        base_cv_id=BaseCvId(UUID(body["base_cv_id"])),
        job_posting_id=JobPostingId(UUID(body["job_posting_id"])),
    )
    active_id = str(active.id.value)  # a refused request expires loaded instances: read it first
    await SqlAlchemyTailoringRunRepository(session).add(active)
    await session.commit()

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "tailoring_already_running"
    assert error_body(response)["active_tailoring_run_id"] == active_id
    assert await balance_of(session, account.user_id.value) == 3


# ------------------------------------------------------------------- F-10: the broker refuses


async def test_f10_a_refused_enqueue_is_503_and_the_second_transaction_refunds_and_counts(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    queue_on(app, refuse=True)
    account, body = await funded(client, session, settings)  # balance 1
    failed_before = await counter_total(session, "runs_failed")

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "queue_unavailable"
    run = (
        await session.execute(
            text("SELECT id, status, failure_reason FROM tailoring_run WHERE user_id = :u"),
            {"u": account.user_id.value},
        )
    ).one()
    assert (run.status, run.failure_reason) == ("failed", "not_queued")
    kinds = (
        await session.execute(
            text(
                "SELECT kind, tailoring_run_id FROM billing_credit_entry "
                "WHERE user_id = :u AND kind IN ('charge', 'refund') ORDER BY created_at, kind"
            ),
            {"u": account.user_id.value},
        )
    ).all()
    assert sorted(k.kind for k in kinds) == ["charge", "refund"]
    assert {k.tailoring_run_id for k in kinds} == {run.id}, "the refund names the run it refunds"
    assert await balance_of(session, account.user_id.value) == 1, "the credit is back"
    assert await counter_total(session, "runs_failed") - failed_before == 1


def logged(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """`caplog`, not `capture_logs()`: a cached module logger escapes the latter (test_intake.py)."""
    lines = []
    for record in caplog.records:
        try:
            lines.append(json.loads(record.getMessage()))
        except ValueError:
            continue
    return lines


def released(caplog: pytest.LogCaptureFixture) -> set[str]:
    return {line["event_type"] for line in logged(caplog) if line.get("event") == "domain_event"}


async def test_f10_the_second_transactions_events_are_published_after_its_commit(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue_on(app, refuse=True)
    account, body = await funded(client, session, settings)

    with caplog.at_level(logging.INFO):
        response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 503, response.text
    assert {"TailoringRunFailed", "CreditRefunded"} <= released(caplog)


async def test_f10_a_failed_second_commit_publishes_no_failure_or_refund_event(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue_on(app, refuse=True)
    account, body = await funded(client, session, settings)
    real_commit = session.commit
    commits = 0

    async def _second_fails() -> None:
        nonlocal commits
        commits += 1
        if commits == 2:
            raise RuntimeError("simulated commit failure (F-11)")
        await real_commit()

    monkeypatch.setattr(session, "commit", _second_fails)

    with caplog.at_level(logging.INFO):
        response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 503, response.text
    assert commits == 2
    assert not {"TailoringRunFailed", "CreditRefunded"} & released(caplog)
    assert "tailoring.not_queued_unrecorded" in [line.get("event") for line in logged(caplog)]


# ===================================================================================== the races


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path / "uploads")
    yield w
    await w.cleanup()


async def committed_inputs(
    engine: AsyncEngine, owner: Owner, *, balance: int, user_id: UUID
) -> dict[str, str]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        await reset_ledger(seeding, user_id, extra=balance - 1)
        await seeding.commit()
        return await inputs(seeding, owner)


async def second_posting(engine: AsyncEngine, owner: Owner) -> str:
    posting = pasted_posting(owner, SystemClock().now() - timedelta(minutes=5), marker="q")
    posting_id = str(posting.id.value)
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        await SqlAlchemyJobPostingRepository(seeding).add(posting)
        await seeding.commit()
    return posting_id


@pytest.mark.parametrize("staged", [True, False], ids=["staged-at-the-lock", "same-posting"])
@pytest.mark.parametrize(
    ("balance", "loser_codes"),
    [(2, {"tailoring_already_running"}), (1, {"tailoring_already_running", "no_credits"})],
    ids=["balance-2", "balance-1"],
)
async def test_ac8_f2_two_concurrent_requests_make_one_run_and_one_charge_and_the_other_is_409(
    world: World,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    balance: int,
    loser_codes: set[str],
    staged: bool,
) -> None:
    """**`staged`**: two postings, so the 2.3 posting lock (which today serializes two runs over *one*
    posting before the account lock is ever reached) does not hide the race. Both requests have then
    inserted their run and neither has taken the account lock when the barrier releases; the database
    serializes them on the account row, and what the loser sees under the lock is what the hard
    re-check exists to refuse. **`same-posting`** is the double click on one body, unstaged: the
    invariant is the same whichever lock serializes it.

    **Mutation, observed 2026-11-08 and removed:** the hard re-check made blind (a wrapper that makes
    `find_active_for_owner(..., excluding=...)` answer `None`) turns both balance-2 variants red on
    `assert [202, 202] == [202, 409]`. Balance 1 stays green under it, because the charge's own
    `NoCreditsLeft` already refuses the second request: it guards the money, not the hard re-check."""
    queue = FakeTailoringQueue()
    world.app.dependency_overrides[get_tailoring_queue] = lambda: queue
    account = await world.account()
    body = await committed_inputs(
        engine, account.owner, balance=balance, user_id=account.user_id.value
    )
    bodies = [body, {**body, "job_posting_id": await second_posting(engine, account.owner)}]
    if not staged:
        bodies = [body, body]
    barrier = asyncio.Barrier(2)
    reached: list[bool] = []
    original = SqlAlchemyCreditAccountRepository.lock_for_charge

    async def _both_reach_the_lock(self: Any, user_id: Any) -> Any:
        reached.append(True)
        await asyncio.wait_for(barrier.wait(), timeout=_STEP / 2)
        return await original(self, user_id)

    if staged:
        monkeypatch.setattr(
            SqlAlchemyCreditAccountRepository, "lock_for_charge", _both_reach_the_lock
        )

    async def request(payload: dict[str, str]) -> Response:
        async with new_client(world.app) as c:
            return await c.post(ME_RUNS, json=payload, headers=account.headers)

    first, second = await asyncio.wait_for(
        asyncio.gather(request(bodies[0]), request(bodies[1])), _STEP
    )

    if staged:
        assert len(reached) == 2, "both requests must reach the lock, or this staged no race"
    assert sorted([first.status_code, second.status_code]) == [202, 409], (first.text, second.text)
    winner, loser = (first, second) if first.status_code == 202 else (second, first)
    assert error_code(loser) in loser_codes
    if error_code(loser) == "tailoring_already_running":
        assert error_body(loser)["active_tailoring_run_id"] == winner.json()["id"]
    async with engine.connect() as conn:
        runs = (
            await conn.execute(
                text("SELECT id FROM tailoring_run WHERE user_id = :u"),
                {"u": account.user_id.value},
            )
        ).all()
        charges = (
            await conn.execute(
                text(
                    "SELECT tailoring_run_id FROM billing_credit_entry "
                    "WHERE user_id = :u AND kind = 'charge'"
                ),
                {"u": account.user_id.value},
            )
        ).all()
    assert [str(r.id) for r in runs] == [winner.json()["id"]]
    assert [str(c.tailoring_run_id) for c in charges] == [winner.json()["id"]]
    assert [str(r.value) for r in queue.enqueued] == [winner.json()["id"]]


async def test_ac13_f15_a_refund_commits_while_a_charge_holds_the_lock_and_nothing_is_lost(
    world: World, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = FakeTailoringQueue()
    world.app.dependency_overrides[get_tailoring_queue] = lambda: queue
    account = await world.account()
    user_id = account.user_id
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        # opening +1, grant +1, one charge for `earlier` (balance 1): the refund below gives it back
        (earlier,) = await reset_ledger(seeding, user_id.value, extra=1, charged=1)
        await seeding.commit()
        body = await inputs(seeding, account.owner)
    holding = asyncio.Event()
    release = asyncio.Event()
    original = SqlAlchemyCreditAccountRepository.lock_for_charge

    async def _hold_the_lock(self: Any, uid: Any) -> Any:
        account_ = await original(self, uid)
        holding.set()
        await asyncio.wait_for(release.wait(), timeout=_STEP)
        return account_

    monkeypatch.setattr(SqlAlchemyCreditAccountRepository, "lock_for_charge", _hold_the_lock)

    async def request() -> Response:
        async with new_client(world.app) as c:
            return await c.post(ME_RUNS, json=body, headers=account.headers)

    charging = asyncio.create_task(request())
    try:
        await asyncio.wait_for(holding.wait(), _STEP)
        async with async_sessionmaker(engine, expire_on_commit=False)() as other:
            await other.execute(text("SET LOCAL lock_timeout = '5s'"))
            refund = RefundCreditForRun(SqlAlchemyCreditAccountRepository(other))
            await asyncio.wait_for(
                refund(
                    RefundCreditCommand(
                        account.owner, ChargedRunRef(value=earlier), SystemClock().now()
                    )
                ),
                _STEP,
            )
            await other.commit()
    finally:
        release.set()
    response = await asyncio.wait_for(charging, _STEP)

    assert response.status_code == 202, response.text
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT kind, credits FROM billing_credit_entry WHERE user_id = :u "
                    "ORDER BY kind"
                ),
                {"u": user_id.value},
            )
        ).all()
    kinds = sorted(r.kind for r in rows)
    assert kinds == ["charge", "charge", "grant", "refund", "welcome"], kinds
    assert sum(r.credits for r in rows) == 1, "balance is exactly the sum of the entries"
    async with new_client(world.app) as reader:
        credits = await reader.get("/api/me/credits", headers=account.headers)
    assert credits.status_code == 200, credits.text
    assert credits.json()["balance"] == 1


async def test_f16_an_erasure_racing_the_charge_is_401_not_signed_in_and_writes_nothing(
    world: World, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = FakeTailoringQueue()
    world.app.dependency_overrides[get_tailoring_queue] = lambda: queue
    account = await world.account()
    body = await committed_inputs(engine, account.owner, balance=3, user_id=account.user_id.value)
    original_add = SqlAlchemyTailoringRunRepository.add
    erased: list[bool] = []

    async def _add_after_an_erasure(self: Any, run: Any) -> None:
        async with engine.begin() as other:
            gone = await other.execute(
                text("DELETE FROM identity_user WHERE id = :i"), {"i": account.user_id.value}
            )
            erased.append(gone.rowcount == 1)
        await original_add(self, run)

    monkeypatch.setattr(SqlAlchemyTailoringRunRepository, "add", _add_after_an_erasure)

    async with new_client(world.app) as c:
        response = await asyncio.wait_for(
            c.post(ME_RUNS, json=body, headers=account.headers), _STEP
        )

    assert erased == [True], "the erasure must land between authorization and the INSERT"
    assert response.status_code == 401, response.text
    assert error_code(response) == "not_signed_in"
    async with engine.connect() as conn:
        for table in ("tailoring_run", "billing_credit_entry", "billing_credit_account"):
            column = "user_id"
            left = (
                await conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {column} = :u"),  # noqa: S608
                    {"u": account.user_id.value},
                )
            ).scalar_one()
            assert left == 0, table
    assert queue.enqueued == []
