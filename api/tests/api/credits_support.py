"""Shared arrangement for slice 5.2's HTTP tests (T28): the ledger a test wants, the counters it reads.

Arrangement is **SQL**, not the aggregate: a test of `GET /api/me/credits` must not depend on
`CreditAccount.charge` being right, and a raw `INSERT` can build states the aggregate refuses on
purpose (a hundred-entry history, a refund for a run nobody charged). Every row obeys the table's
CHECKs, so a state the database would refuse is never what is under test.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tailorcraft.infrastructure.clock import SystemClock

ME_CREDITS = "/api/me/credits"
ME_PACK_CLICKS = "/api/me/pack-clicks"
ME_PAYMENT_NOTICE = "/api/me/payment-notice"
ME_RUNS = "/api/me/tailoring-runs"
ADMIN_COUNTERS = "/api/admin/demand-counters"

COUNTER_NAMES = (
    "fit_scores",
    "fit_scores_busy",
    "sign_ups",
    "pack_clicks",
    "pack_clickers",
    "runs_failed",
    "runs_succeeded",
)

_INSERT_ENTRY = text(
    "INSERT INTO billing_credit_entry (id, user_id, kind, credits, tailoring_run_id, created_at) "
    "VALUES (:id, :u, :kind, :credits, :run, :at)"
)


def fit_scores_url(run_id: object, side: str | None = None) -> str:
    base = f"{ME_RUNS}/{run_id}/fit-scores"
    return base if side is None else f"{base}/{side}"


async def reset_ledger(
    session: AsyncSession | AsyncConnection,
    user_id: UUID,
    *,
    opening: str = "welcome",
    extra: int = 0,
    charged: int = 0,
    refunded: int = 0,
    extra_rows: int = 0,
) -> list[UUID]:
    """Replace the account's whole ledger: the opening entry (+1), one `grant` of `extra`, `charged`
    charges (the first `refunded` of them refunded), and `extra_rows` one-credit grants on top.
    Returns the charged run ids, oldest first. Balance = 1 + extra + extra_rows - charged + refunded.
    Whole-second, strictly increasing `created_at`, so "newest first" is unambiguous. Never commits:
    the caller owns the transaction (a shared test session's commit releases a SAVEPOINT)."""
    assert 0 <= extra <= 100
    assert 0 <= refunded <= charged
    base = SystemClock().now() - timedelta(hours=2)
    n = 0

    async def put(kind: str, credits: int, run: UUID | None = None) -> None:
        nonlocal n
        n += 1
        await session.execute(
            _INSERT_ENTRY,
            {
                "id": uuid4(),
                "u": user_id,
                "kind": kind,
                "credits": credits,
                "run": run,
                "at": base + timedelta(seconds=n),
            },
        )

    await session.execute(
        text("DELETE FROM billing_credit_entry WHERE user_id = :u"), {"u": user_id}
    )
    await put(opening, 1)
    if extra:
        await put("grant", extra)
    runs = [uuid4() for _ in range(charged)]
    for run in runs:
        await put("charge", -1, run)
    for run in runs[:refunded]:
        await put("refund", 1, run)
    for _ in range(extra_rows):
        await put("grant", 1)
    if isinstance(session, AsyncSession):
        session.expire_all()
    return runs


async def counter_total(session: AsyncSession | AsyncConnection, counter: str) -> int:
    """Everything counted for `counter` on any day. Tests compare before and after, so a leftover row
    from another test's committed world cannot move the answer."""
    result = await session.execute(
        text("SELECT COALESCE(SUM(count), 0) FROM demand_daily_counter WHERE counter = :c"),
        {"c": counter},
    )
    return int(result.scalar_one())


async def all_counter_totals(session: AsyncSession | AsyncConnection) -> dict[str, int]:
    return {name: await counter_total(session, name) for name in COUNTER_NAMES}
