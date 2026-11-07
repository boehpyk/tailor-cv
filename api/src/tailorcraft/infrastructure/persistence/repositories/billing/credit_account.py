"""`SqlAlchemyCreditAccountRepository` — the `CreditAccountRepository` port (ADR-0007, ADR-0042).

Filters use the mapped `Table`'s columns, never `CreditAccount.user_id` (a plain `@property`, which would
build `WHERE true` or `WHERE false`; `repositories/tailoring/tailoring_run.py`'s module note).

**The lock** (plan §0.3). `with_for_update(key_share=True)` renders **`FOR NO KEY UPDATE`**, on purpose:
it conflicts with another charge's, grant's or pack click's lock and with erasure's `DELETE`, and **not**
with the `FOR KEY SHARE` a refund's foreign-key check takes, so a refund never waits on a charge. (The
same call is CLAUDE.md's footgun when `FOR KEY SHARE` was meant; here `NO KEY UPDATE` *is* meant.) The
compiled SQL is pinned by a test. `populate_existing` makes the locked read refresh an account this
session already holds, entries included, so a charge that waited on the lock judges the balance the
previous holder committed, never a copy loaded before the wait.

**Entries are only ever inserted** (CR-6, AC-3). `save` adds the account; the relationship's
`save-update` cascade inserts the appended entries; nothing here issues an `UPDATE` or `DELETE` on
`billing_credit_entry`. The account row's own `UPDATE` (a pack click, an opt-in) is the only update.

Nothing commits: the caller's transaction does (the request's, or the failure's in the worker).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.errors import CreditAccountNotFound
from tailorcraft.domain.billing.value_objects import CreditEntryId
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.mapping.billing.credit_account import (
    credit_account_table,
)

if TYPE_CHECKING:
    from sqlalchemy import Select

    from tailorcraft.domain.billing.ports import CreditAccountRepository


def lock_for_charge_statement(user_id: UserId) -> Select[tuple[CreditAccount]]:
    """The locking read, separate so a test can compile it and pin `FOR NO KEY UPDATE`."""
    return (
        select(CreditAccount)
        .where(credit_account_table.c.user_id == user_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )


class SqlAlchemyCreditAccountRepository:
    """Persistence for `CreditAccount`, backed by `billing_credit_account` and `billing_credit_entry`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_entry_identity(self) -> CreditEntryId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return CreditEntryId(uuid7())

    async def add(self, account: CreditAccount) -> None:
        await self.save(account)

    async def find(self, user_id: UserId) -> CreditAccount | None:
        return (
            await self._session.execute(
                select(CreditAccount).where(credit_account_table.c.user_id == user_id)
            )
        ).scalar_one_or_none()

    async def lock_for_charge(self, user_id: UserId) -> CreditAccount:
        account = (
            await self._session.execute(lock_for_charge_statement(user_id))
        ).scalar_one_or_none()
        if account is None:
            raise CreditAccountNotFound(user_id)
        return account

    async def save(self, account: CreditAccount) -> None:
        self._session.add(account)
        await self._session.flush()
        account.mark_entries_saved()


if TYPE_CHECKING:

    def _assert_implements_credit_account_repository(
        repo: SqlAlchemyCreditAccountRepository,
    ) -> None:
        _: CreditAccountRepository = repo
