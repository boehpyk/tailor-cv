"""`SqlAlchemyCreditAccountRepository` against a real Postgres (slice 5.2, T21, test-after).

The pinned lock SQL and a round trip. The rest (AC-3's SQL capture, each CHECK and unique index
refusing, F-15 with `concurrent_app`) is T30's and the held specifications in
`tests/integration/billing/test_pending_adapters.py`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.errors import RunAlreadyRefunded
from tailorcraft.domain.billing.value_objects import (
    ChargedRunRef,
    CreditEntryKind,
    GrantAmount,
    OpeningGrant,
)
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.mapping.billing.credit_account import (
    credit_entry_table,
)
from tailorcraft.infrastructure.persistence.repositories.billing.credit_account import (
    SqlAlchemyCreditAccountRepository,
    lock_for_charge_statement,
)
from tests.integration.persistence.owner_rows import persist_user


async def test_the_charge_lock_compiles_to_for_no_key_update(session: AsyncSession) -> None:
    """Plan §0.3: `NO KEY UPDATE` conflicts with another charge and with erasure's delete, and not
    with the `KEY SHARE` a refund's foreign-key check takes. `FOR UPDATE` would make refunds wait on
    charges; `FOR KEY SHARE` would let two charges spend one credit."""
    statement = lock_for_charge_statement(UserId(uuid7()))
    sql = str(statement.compile(dialect=session.get_bind().dialect)).split()

    assert sql[-4:] == ["FOR", "NO", "KEY", "UPDATE"]


async def test_an_opened_account_round_trips_with_its_entries_oldest_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    credits = SqlAlchemyCreditAccountRepository(session)
    run = ChargedRunRef(uuid7())
    account = CreditAccount.open(
        user_id=owner.user_id,
        opening=OpeningGrant.WELCOME,
        entry_id=credits.next_entry_identity(),
        at=clock.now(),
    )
    await credits.add(account)
    assert account.pending_entries() == ()

    # One second apart: the history orders by (created_at, id), and UUIDv7 is only ms-ordered.
    clock.advance(1)
    locked = await credits.lock_for_charge(owner.user_id)
    locked.charge(run, entry_id=credits.next_entry_identity(), at=clock.now())
    clock.advance(1)
    locked.grant(GrantAmount(3), entry_id=credits.next_entry_identity(), at=clock.now())
    await credits.save(locked)
    session.expunge_all()

    found = await credits.find(owner.user_id)

    assert found is not None
    assert [e.kind for e in found.entries] == [
        CreditEntryKind.WELCOME,
        CreditEntryKind.CHARGE,
        CreditEntryKind.GRANT,
    ]
    assert found.entries[1].run == run
    assert found.balance == 3
    assert found.notice is None


async def test_find_answers_none_for_a_user_without_an_account(session: AsyncSession) -> None:
    assert await SqlAlchemyCreditAccountRepository(session).find(UserId(uuid7())) is None


async def test_a_refund_losing_the_unique_index_race_is_run_already_refunded_and_the_session_lives(
    session: AsyncSession, clock: FixedClock
) -> None:
    """F-14: the account was loaded before another refund of the same run committed, so CR-4 passes
    in memory and only `uq_billing_credit_entry_refund_run` refuses. The repository answers
    `RunAlreadyRefunded`, and the session is usable for the sweep's next run."""
    owner = await persist_user(session, clock)
    credits = SqlAlchemyCreditAccountRepository(session)
    run = ChargedRunRef(uuid7())
    account = CreditAccount.open(
        user_id=owner.user_id,
        opening=OpeningGrant.WELCOME,
        entry_id=credits.next_entry_identity(),
        at=clock.now(),
    )
    account.charge(run, entry_id=credits.next_entry_identity(), at=clock.now())
    await credits.add(account)
    await session.commit()
    session.expunge_all()

    loser = await credits.find(owner.user_id)
    assert loser is not None
    # The winner's refund, committed after the loser loaded the account.
    await session.execute(
        insert(credit_entry_table).values(
            id=credits.next_entry_identity(),
            user_id=owner.user_id,
            kind=CreditEntryKind.REFUND,
            credits=1,
            tailoring_run_id=run,
            created_at=clock.now(),
        )
    )
    await session.commit()
    assert loser.refund(run, entry_id=credits.next_entry_identity(), at=clock.now())

    with pytest.raises(RunAlreadyRefunded) as raised:
        await credits.save(loser)

    assert raised.value.run == run
    found = await credits.find(owner.user_id)
    assert found is not None
    assert [e.kind for e in found.entries].count(CreditEntryKind.REFUND) == 1
    assert found.balance == 1
