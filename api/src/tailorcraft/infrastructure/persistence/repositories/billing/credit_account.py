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

**Reads never autoflush** (F-14). A read here must not write what the caller dirtied: in the worker
that is the run `RecordRunFailure` has just marked failed, and an autoflush on the refund's `find`
would raise its version conflict as a raw `StaleDataError` from inside this repository, outside the
run repository's translation. Under `no_autoflush` the conflict surfaces at `runs.save`, where it is
translated to `TailoringRunConcurrentlyModified` and the worker rolls the whole transaction back,
refund and count included. `save` restricts its flush to this aggregate for the same reason. Nothing
this repository reads can be pending in the session: every `save` flushes.

Nothing commits: the caller's transaction does (the request's, or the failure's in the worker).
The one exception is F-14's refund race, below, which rolls back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.errors import CreditAccountNotFound, RunAlreadyRefunded
from tailorcraft.domain.billing.value_objects import CreditEntryId, CreditEntryKind
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.billing.credit_account import (
    credit_account_table,
)

if TYPE_CHECKING:
    from sqlalchemy import Select

    from tailorcraft.domain.billing.ports import CreditAccountRepository

# Recognised by name, never by message (`violated_constraint`). Renaming the index in a migration
# without changing this turns F-14 back into an untranslated `IntegrityError`.
_REFUND_RUN_UNIQUE = "uq_billing_credit_entry_refund_run"


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
        with self._session.no_autoflush:
            return (
                await self._session.execute(
                    select(CreditAccount).where(credit_account_table.c.user_id == user_id)
                )
            ).scalar_one_or_none()

    async def lock_for_charge(self, user_id: UserId) -> CreditAccount:
        with self._session.no_autoflush:
            account = (
                await self._session.execute(lock_for_charge_statement(user_id))
            ).scalar_one_or_none()
        if account is None:
            raise CreditAccountNotFound(user_id)
        return account

    async def save(self, account: CreditAccount) -> None:
        """Insert the pending entries (and the account, when new); `RunAlreadyRefunded` on F-14.

        **The flush is restricted to this aggregate's objects.** A bare `flush()` would also write
        whatever else the caller dirtied: in the worker that is the run `RecordRunFailure` has just
        marked failed, whose version check then fires here, outside the run repository's
        `StaleDataError` translation and its SAVEPOINT. The run is the run repository's to write.

        **F-14 rolls back the whole transaction, then raises `RunAlreadyRefunded`.** The losing
        refund's `INSERT` waits on the winner's uncommitted index entry and fails once it commits.
        A SAVEPOINT would contain the aborted statement but not the loser's own `mark_failed`, which
        is still dirty in the session: the sweep's next `runs.save` (its `begin_nested()` flushes on
        entry) would write it, outside any SAVEPOINT, and fail on its version. The rollback discards
        both, which is what the worker does for any failed save (T23); it also expires every loaded
        instance, which the sweep survives because it re-reads each run by id. Only refunds can
        reach this index. The worker refunds, and so does `RecordRunNotQueued`, in the API's second
        transaction (G-14): there the rollback takes that transaction's run update and count too,
        which `record_not_queued`'s `except` already treats as "the run stays `queued`". Any other
        refusal propagates untranslated.
        """
        pending = account.pending_entries()
        # Read before the flush: a failed flush expires what the session holds.
        refunded = next((e.run for e in pending if e.kind is CreditEntryKind.REFUND), None)
        self._session.add(account)
        try:
            await self._session.flush([account, *pending])
        except IntegrityError as exc:
            if refunded is None or violated_constraint(exc) != _REFUND_RUN_UNIQUE:
                raise
            await self._session.rollback()
            raise RunAlreadyRefunded(refunded) from None
        account.mark_entries_saved()


if TYPE_CHECKING:

    def _assert_implements_credit_account_repository(
        repo: SqlAlchemyCreditAccountRepository,
    ) -> None:
        _: CreditAccountRepository = repo
