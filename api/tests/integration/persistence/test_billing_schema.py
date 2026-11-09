"""The billing and demand tables as the database enforces them (slice 5.2, T30, test-after).

Round trip of the aggregate's account columns, AC-3's SQL capture, each CHECK and partial unique index
refusing **by name** (`violated_constraint`, never the driver's message), the catalogue facts AC-2 and
AC-28 name, the cascade (AC-31) and what the guest purge leaves (AC-28). Rows are written as SQL where a
refusal is the point, so the aggregate's own invariants are not in front of the constraint under test.

**`ck_billing_credit_entry_kind_known` is not separately observable.** PostgreSQL evaluates CHECKs in
name order and `…_credits_by_kind` sorts first, and an unknown kind matches none of its branches, so the
unknown-kind row is always reported as `credits_by_kind`. The test says that, and the catalogue test
pins that `kind_known` exists with the right members.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.retention.purge_expired_guest_sessions import (
    PurgeExpiredGuestSessions,
)
from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.value_objects import (
    ChargedRunRef,
    CreditEntryKind,
    GrantAmount,
    OpeningGrant,
    Pack,
)
from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.demand.daily_counter import SqlAlchemyDailyCounter
from tailorcraft.infrastructure.persistence.repositories.billing.credit_account import (
    SqlAlchemyCreditAccountRepository,
)
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.retention.data_access import CommittingExpiredGuestDataAdapter
from tests.integration.persistence.owner_rows import persist_user
from tests.integration.retention.test_purge_database import _Rig


@pytest.fixture
def rig(session: AsyncSession, clock: FixedClock, tmp_path: Path) -> _Rig:
    return _Rig(session=session, clock=clock, files=LocalFileStore(tmp_path))


async def _open_account(session: AsyncSession, clock: FixedClock) -> UUID:
    """A user and its `billing_credit_account` row (no entry), as SQL. Flushed in the test's txn."""
    owner = await persist_user(session, clock)
    await session.execute(
        text("INSERT INTO billing_credit_account (user_id, opened_at) VALUES (:u, :t)"),
        {"u": owner.user_id.value, "t": clock.now()},
    )
    return owner.user_id.value


async def _entry(
    session: AsyncSession,
    clock: FixedClock,
    user_id: UUID,
    *,
    kind: str,
    credits: int,
    run: UUID | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO billing_credit_entry (id, user_id, kind, credits, tailoring_run_id, "
            "created_at) VALUES (:i, :u, :k, :c, :r, :t)"
        ),
        {"i": uuid7(), "u": user_id, "k": kind, "c": credits, "r": run, "t": clock.now()},
    )


async def _refused_by(
    session: AsyncSession, clock: FixedClock, user_id: UUID, **entry: Any
) -> str | None:
    with pytest.raises(IntegrityError) as caught:
        async with session.begin_nested():
            await _entry(session, clock, user_id, **entry)
    return violated_constraint(caught.value)


# --- the round trip ---------------------------------------------------------------------------------


async def test_the_accounts_own_columns_round_trip_through_the_repository(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    credits = SqlAlchemyCreditAccountRepository(session)
    account = CreditAccount.open(
        user_id=owner.user_id,
        opening=OpeningGrant.EXISTING_ACCOUNT,
        entry_id=credits.next_entry_identity(),
        at=clock.now(),
    )
    await credits.add(account)
    clock.advance(5)
    assert account.record_pack_click(Pack.PACK_3, at=clock.now())
    clock.advance(5)
    account.choose_payment_notice(True, at=clock.now())
    await credits.save(account)
    expected_click, expected_opt_in = (
        account.first_pack_click_at,
        account.payment_notice_opted_in_at,
    )
    session.expunge_all()

    found = await credits.find(owner.user_id)

    assert found is not None
    assert found is not account
    assert found.first_pack_click_at == expected_click
    assert found.payment_notice_opted_in_at == expected_opt_in
    assert expected_click != expected_opt_in, "premise: two distinct instants"
    assert [e.kind for e in found.entries] == [CreditEntryKind.EXISTING_ACCOUNT]
    assert found.entries[0].at.microsecond == 0


# --- AC-3: no UPDATE or DELETE on an entry, ever ----------------------------------------------------

_MUTATES_ENTRIES = re.compile(
    r"^\s*(UPDATE|DELETE\s+FROM)\s+(ONLY\s+)?\"?billing_credit_entry\b", re.IGNORECASE
)


async def _captured(session: AsyncSession, work: Any) -> list[str]:
    statements: list[str] = []
    connection = await session.connection()

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    event.listen(connection.sync_connection, "before_cursor_execute", record)
    try:
        await work()
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", record)
    return statements


async def test_the_detector_sees_an_update_and_a_delete_of_an_entry_so_the_absence_below_counts(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _open_account(session, clock)
    await _entry(session, clock, user_id, kind="welcome", credits=1)

    async def mutate() -> None:
        await session.execute(text("UPDATE billing_credit_entry SET credits = 1"))
        await session.execute(
            text("DELETE FROM billing_credit_entry WHERE user_id = :u"), {"u": user_id}
        )

    statements = await _captured(session, mutate)

    assert [bool(_MUTATES_ENTRIES.match(s)) for s in statements] == [True, True]


async def test_ac3_a_full_life_of_an_account_issues_no_update_or_delete_on_entries(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    credits = SqlAlchemyCreditAccountRepository(session)
    run = ChargedRunRef(uuid7())

    async def life() -> None:
        account = CreditAccount.open(
            user_id=owner.user_id,
            opening=OpeningGrant.WELCOME,
            entry_id=credits.next_entry_identity(),
            at=clock.now(),
        )
        await credits.add(account)
        locked = await credits.lock_for_charge(owner.user_id)
        locked.charge(run, entry_id=credits.next_entry_identity(), at=clock.now())
        locked.grant(GrantAmount(2), entry_id=credits.next_entry_identity(), at=clock.now())
        locked.record_pack_click(Pack.PACK_10, at=clock.now())
        locked.choose_payment_notice(True, at=clock.now())
        await credits.save(locked)
        again = await credits.lock_for_charge(owner.user_id)
        assert again.refund(run, entry_id=credits.next_entry_identity(), at=clock.now())
        await credits.save(again)
        await credits.save(again)
        await session.flush()

    statements = await _captured(session, life)

    inserted = [
        s for s in statements if s.lstrip().upper().startswith("INSERT INTO BILLING_CREDIT_ENTRY")
    ]
    assert len(inserted) >= 3, "premise: the entries really were written"
    assert [s for s in statements if _MUTATES_ENTRIES.match(s)] == []
    found = await credits.find(owner.user_id)
    assert found is not None
    assert found.balance == 3  # welcome +1, charge -1, grant +2, refund +1


# --- each CHECK refuses -----------------------------------------------------------------------------

_CREDITS_BY_KIND = "ck_billing_credit_entry_credits_by_kind"
_RUN_IFF = "ck_billing_credit_entry_run_iff_charge_or_refund"
_KIND_KNOWN = "ck_billing_credit_entry_kind_known"


@pytest.mark.parametrize(
    ("kind", "credits", "has_run"),
    [
        ("welcome", 0, False),
        ("welcome", 2, False),
        ("existing_account", -1, False),
        ("grant", 0, False),
        ("grant", 101, False),
        ("grant", -5, False),
        ("charge", 1, True),
        ("charge", -2, True),
        ("refund", -1, True),
        ("refund", 2, True),
        ("bonus", 1, False),
    ],
)
async def test_credits_by_kind_refuses_a_wrong_amount_for_the_kind(
    session: AsyncSession, clock: FixedClock, kind: str, credits: int, has_run: bool
) -> None:
    user_id = await _open_account(session, clock)

    name = await _refused_by(
        session, clock, user_id, kind=kind, credits=credits, run=uuid4() if has_run else None
    )

    assert name == _CREDITS_BY_KIND, (
        "an unknown kind is reported here too: CHECKs run in name order (module docstring)"
    )


@pytest.mark.parametrize(
    ("kind", "credits", "has_run"),
    [
        ("charge", -1, False),
        ("refund", 1, False),
        ("welcome", 1, True),
        ("existing_account", 1, True),
        ("grant", 3, True),
    ],
)
async def test_run_iff_charge_or_refund_refuses_a_missing_or_stray_run(
    session: AsyncSession, clock: FixedClock, kind: str, credits: int, has_run: bool
) -> None:
    user_id = await _open_account(session, clock)

    name = await _refused_by(
        session, clock, user_id, kind=kind, credits=credits, run=uuid4() if has_run else None
    )

    assert name == _RUN_IFF


async def test_every_valid_kind_amount_and_run_combination_is_accepted(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The control for the refusals above: the same helper, valid rows, one per distinct user so the
    opening index is not in the way."""
    for kind, credits, run in [
        ("welcome", 1, None),
        ("existing_account", 1, None),
        ("grant", 1, None),
        ("grant", 100, None),
        ("charge", -1, uuid4()),
        ("refund", 1, uuid4()),
    ]:
        user_id = await _open_account(session, clock)
        await _entry(session, clock, user_id, kind=kind, credits=credits, run=run)

    assert (
        await session.execute(text("SELECT count(*) FROM billing_credit_entry"))
    ).scalar_one() >= 6


async def test_the_catalogue_names_every_check_and_the_known_kinds(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE contype = 'c' AND conrelid IN ('billing_credit_entry'::regclass, "
                "'demand_daily_counter'::regclass)"
            )
        )
    ).all()
    checks = {row[0]: row[1] for row in rows}

    assert set(checks) == {
        _CREDITS_BY_KIND,
        _RUN_IFF,
        _KIND_KNOWN,
        "ck_demand_daily_counter_known",
        "ck_demand_daily_counter_non_negative",
    }
    for kind in CreditEntryKind:
        assert f"'{kind.value}'" in checks[_KIND_KNOWN]
    for counter in DemandCounter:
        assert f"'{counter.value}'" in checks["ck_demand_daily_counter_known"]


# --- each partial unique index refuses --------------------------------------------------------------


async def test_a_second_charge_for_one_run_is_refused_by_the_charge_run_index(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _open_account(session, clock)
    run = uuid4()
    await _entry(session, clock, user_id, kind="charge", credits=-1, run=run)

    other_user = await _open_account(session, clock)
    name = await _refused_by(session, clock, other_user, kind="charge", credits=-1, run=run)

    assert name == "uq_billing_credit_entry_charge_run"


async def test_a_second_refund_for_one_run_is_refused_by_the_refund_run_index(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _open_account(session, clock)
    run = uuid4()
    await _entry(session, clock, user_id, kind="refund", credits=1, run=run)

    name = await _refused_by(session, clock, user_id, kind="refund", credits=1, run=run)

    assert name == "uq_billing_credit_entry_refund_run"


async def test_a_run_may_hold_one_charge_and_one_refund(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Control: each index is partial on its own kind, so a charge and its refund coexist."""
    user_id = await _open_account(session, clock)
    run = uuid4()
    await _entry(session, clock, user_id, kind="charge", credits=-1, run=run)
    await _entry(session, clock, user_id, kind="refund", credits=1, run=run)


@pytest.mark.parametrize("second", ["welcome", "existing_account"])
@pytest.mark.parametrize("first", ["welcome", "existing_account"])
async def test_a_second_opening_entry_for_one_account_is_refused_by_the_opening_index(
    session: AsyncSession, clock: FixedClock, first: str, second: str
) -> None:
    user_id = await _open_account(session, clock)
    await _entry(session, clock, user_id, kind=first, credits=1)

    name = await _refused_by(session, clock, user_id, kind=second, credits=1)

    assert name == "uq_billing_credit_entry_opening"


async def test_many_grants_for_one_account_are_allowed(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _open_account(session, clock)
    for _ in range(3):
        await _entry(session, clock, user_id, kind="grant", credits=5)


async def test_the_catalogue_names_the_four_billing_indexes_and_their_predicates(
    session: AsyncSession,
) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN "
                "('billing_credit_entry', 'scoring_fit_score') AND indexname IN "
                "('uq_billing_credit_entry_charge_run', 'uq_billing_credit_entry_refund_run', "
                "'uq_billing_credit_entry_opening', 'ix_billing_credit_entry_user_id_created_at', "
                "'ix_scoring_fit_score_tailoring_run_id')"
            )
        )
    ).all()
    defs = {row[0]: row[1] for row in rows}

    assert set(defs) == {
        "uq_billing_credit_entry_charge_run",
        "uq_billing_credit_entry_refund_run",
        "uq_billing_credit_entry_opening",
        "ix_billing_credit_entry_user_id_created_at",
        "ix_scoring_fit_score_tailoring_run_id",
    }
    assert "UNIQUE" in defs["uq_billing_credit_entry_charge_run"]
    assert "kind" in defs["uq_billing_credit_entry_charge_run"].split("WHERE")[1]
    assert "'charge'" in defs["uq_billing_credit_entry_charge_run"]
    assert "'refund'" in defs["uq_billing_credit_entry_refund_run"]
    assert "'welcome'" in defs["uq_billing_credit_entry_opening"]
    assert "'existing_account'" in defs["uq_billing_credit_entry_opening"]
    assert "UNIQUE" not in defs["ix_billing_credit_entry_user_id_created_at"]
    assert "created_at DESC" in defs["ix_billing_credit_entry_user_id_created_at"]
    assert "tailoring_run_id IS NOT NULL" in defs["ix_scoring_fit_score_tailoring_run_id"]


async def test_an_entry_for_a_user_with_no_account_is_refused_by_its_foreign_key(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)  # a user, but no billing_credit_account

    name = await _refused_by(session, clock, owner.user_id.value, kind="welcome", credits=1)

    assert name == "fk_billing_credit_entry_user_id_billing_credit_account"


# --- the counter table's constraints ----------------------------------------------------------------


async def _counter_row(session: AsyncSession, day: date, counter: str, count: int) -> None:
    await session.execute(
        text("INSERT INTO demand_daily_counter (day, counter, count) VALUES (:d, :c, :n)"),
        {"d": day, "c": counter, "n": count},
    )


@pytest.mark.parametrize(
    ("counter", "count", "second", "expected"),
    [
        ("page_views", 1, False, "ck_demand_daily_counter_known"),
        ("sign_ups", -1, False, "ck_demand_daily_counter_non_negative"),
        ("sign_ups", 1, True, "pk_demand_daily_counter"),
    ],
)
async def test_the_counter_table_refuses_by_name(
    session: AsyncSession, counter: str, count: int, second: bool, expected: str
) -> None:
    day = date(2500, 1, 1) + timedelta(days=uuid4().int % 3000)
    if second:
        await _counter_row(session, day, counter, 1)

    with pytest.raises(IntegrityError) as caught:
        async with session.begin_nested():
            await _counter_row(session, day, counter, count)

    assert violated_constraint(caught.value) == expected


# --- AC-2: the catalogue (the backfill itself is in test_billing_migration.py) -----------------------


async def test_ac2_an_account_is_one_row_per_user_keyed_and_cascaded_on_the_user(
    session: AsyncSession,
) -> None:
    pk = (
        (
            await session.execute(
                text(
                    "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                    "AND a.attnum = ANY (i.indkey) WHERE i.indrelid = 'billing_credit_account'::regclass "
                    "AND i.indisprimary"
                )
            )
        )
        .scalars()
        .all()
    )
    fks = (
        await session.execute(
            text(
                "SELECT confrelid::regclass::text, confdeltype::text FROM pg_constraint "
                "WHERE contype = 'f' AND conrelid = 'billing_credit_account'::regclass"
            )
        )
    ).all()

    assert pk == ["user_id"], "one account per user is a primary key, not a convention"
    assert [tuple(r) for r in fks] == [("identity_user", "c")]


async def test_ac2_a_user_confirmed_by_the_application_has_exactly_one_account(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The other half of 'every user has exactly one account': the application path that makes users
    opens one in the same transaction (`ConfirmRegistration`, AC-1). Driven here as the repository calls
    it makes, then the join the catalogue test would run over a whole database."""
    owner = await persist_user(session, clock)
    credits = SqlAlchemyCreditAccountRepository(session)
    await credits.add(
        CreditAccount.open(
            user_id=owner.user_id,
            opening=OpeningGrant.WELCOME,
            entry_id=credits.next_entry_identity(),
            at=clock.now(),
        )
    )
    await session.flush()

    counts = (
        await session.execute(
            text(
                "SELECT count(a.user_id) FROM identity_user u LEFT JOIN billing_credit_account a "
                "ON a.user_id = u.id WHERE u.id = :u"
            ),
            {"u": owner.user_id.value},
        )
    ).scalar_one()
    assert counts == 1


# --- AC-28: the counter table is anonymous and survives retention -----------------------------------


async def test_ac28_the_counter_table_has_exactly_day_counter_count_and_no_reference(
    session: AsyncSession,
) -> None:
    columns = (
        await session.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'demand_daily_counter' AND table_schema = 'public' "
                "ORDER BY ordinal_position"
            )
        )
    ).all()
    references = (
        await session.execute(
            text(
                "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
                "AND conrelid = 'demand_daily_counter'::regclass"
            )
        )
    ).scalar_one()

    assert [tuple(c) for c in columns] == [
        ("day", "date"),
        ("counter", "character varying"),
        ("count", "bigint"),
    ]
    assert not [c for c, t in columns if t == "uuid" or re.search(r"id$|owner|user|session", c)]
    assert references == 0


async def test_ac28_erasing_an_account_leaves_the_counters_and_takes_the_ledger(
    session: AsyncSession, clock: FixedClock
) -> None:
    day = date(2600, 1, 1) + timedelta(days=uuid4().int % 3000)
    user_id = await _open_account(session, clock)
    await _entry(session, clock, user_id, kind="welcome", credits=1)
    await _entry(session, clock, user_id, kind="grant", credits=4)
    await SqlAlchemyDailyCounter(session).increment(DemandCounter.SIGN_UPS, day)

    from tailorcraft.domain.identity.value_objects import UserId

    assert await SqlAlchemyAccountData(session).delete_account(UserId(user_id))

    async def count(sql: str, key: object) -> int:
        return int((await session.execute(text(sql), {"k": key})).scalar_one())

    assert (
        await count("SELECT count(*) FROM billing_credit_entry WHERE user_id = :k", user_id) == 0
    ), "AC-31: the cascade takes every entry"
    assert (
        await count("SELECT count(*) FROM billing_credit_account WHERE user_id = :k", user_id) == 0
    )
    assert await count("SELECT count(*) FROM demand_daily_counter WHERE day = :k", day) == 1, (
        "AC-28: counters survive erasure"
    )


async def test_ac28_the_guest_purge_leaves_every_billing_row_of_a_registered_user_and_the_counters(
    session: AsyncSession, rig: _Rig, clock: FixedClock, tmp_path: Path
) -> None:
    now = clock.now()
    day = date(2700, 1, 1) + timedelta(days=uuid4().int % 3000)
    expired = await rig.new_guest_session(expires_at=now - timedelta(hours=30))
    user_id = await _open_account(session, clock)
    await _entry(session, clock, user_id, kind="welcome", credits=1)
    await _entry(session, clock, user_id, kind="charge", credits=-1, run=uuid4())
    await _entry(session, clock, user_id, kind="grant", credits=2)
    await SqlAlchemyDailyCounter(session).increment(DemandCounter.PACK_CLICKS, day)
    await session.commit()

    result = await PurgeExpiredGuestSessions(
        data=CommittingExpiredGuestDataAdapter(SqlAlchemyExpiredGuestData(session), session),
        files=LocalFileStore(tmp_path),
        clock=clock,
        window=RetentionWindow(hours=24),
        batch_limit=10,
        dry_run=False,
    )()

    assert result.sessions_deleted >= 1, (
        "premise: the purge really ran and took the expired session"
    )
    assert (
        await session.execute(
            text("SELECT count(*) FROM identity_guest_session WHERE id = :g"), {"g": expired.value}
        )
    ).scalar_one() == 0
    kept = (
        (
            await session.execute(
                text("SELECT kind FROM billing_credit_entry WHERE user_id = :u ORDER BY kind"),
                {"u": user_id},
            )
        )
        .scalars()
        .all()
    )
    assert kept == ["charge", "grant", "welcome"]
    assert (
        await session.execute(
            text("SELECT count(*) FROM billing_credit_account WHERE user_id = :u"), {"u": user_id}
        )
    ).scalar_one() == 1
    assert (
        await session.execute(
            text("SELECT count FROM demand_daily_counter WHERE day = :d"), {"d": day}
        )
    ).scalar_one() == 1


# --- AC-31 (adapter half): count_account's billing columns ------------------------------------------


async def test_ac31_count_account_reports_the_balance_and_the_entry_count(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _open_account(session, clock)
    await _entry(session, clock, user_id, kind="welcome", credits=1)
    await _entry(session, clock, user_id, kind="grant", credits=5)
    await _entry(session, clock, user_id, kind="charge", credits=-1, run=uuid4())
    stranger = await _open_account(session, clock)
    await _entry(session, clock, stranger, kind="grant", credits=50)
    from tailorcraft.domain.identity.value_objects import UserId

    counts = await SqlAlchemyAccountData(session).count_account(UserId(user_id))

    assert counts is not None
    assert (counts.credits_left, counts.credit_entries) == (5, 3)


async def test_ac31_count_account_reports_zero_and_zero_for_an_account_with_no_ledger(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`COALESCE(SUM, 0)`: a user with no entries reads 0, never `None`."""
    owner = await persist_user(session, clock)

    counts = await SqlAlchemyAccountData(session).count_account(owner.user_id)

    assert counts is not None
    assert (counts.credits_left, counts.credit_entries) == (0, 0)
