"""Ports the `billing` context needs from the outside world, in the domain's own language.

No red-first cycle: a `Protocol` has no behaviour to fail (docs/sdlc.md §2). The adapter satisfies this
structurally under `mypy --strict`, and `test_port_bindings.py` holds it to a composition root.
"""

from __future__ import annotations

from typing import Protocol

from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.value_objects import CreditEntryId
from tailorcraft.domain.identity.value_objects import UserId


class CreditAccountRepository(Protocol):
    """Persistence for `CreditAccount`: one account per user, its entries loaded with it (plan §0.2).

    Says nothing about transactions: every method runs on the caller's, and none commits.
    """

    def next_entry_identity(self) -> CreditEntryId:
        """Mint an id for an entry that does not exist yet. Needs no I/O (UUIDv7, ADR-0007)."""
        ...

    async def add(self, account: CreditAccount) -> None:
        """Insert a newly opened account with its pending entries."""
        ...

    async def find(self, user_id: UserId) -> CreditAccount | None:
        """The user's account, or `None`. Takes no lock: for reads and for the refund (plan §0.4)."""
        ...

    async def lock_for_charge(self, user_id: UserId) -> CreditAccount:
        """The user's account, row-locked `FOR NO KEY UPDATE` until the transaction ends, so two
        charges, grants or clicks for one user serialize. `CreditAccountNotFound` when there is none."""
        ...

    async def save(self, account: CreditAccount) -> None:
        """Insert `pending_entries()`, update the account row, then `mark_entries_saved()`.

        **May roll back the caller's whole transaction.** A refund for a run already refunded (F-14)
        rolls the transaction back, then raises `RunAlreadyRefunded`: everything else the caller
        wrote in it is gone, and every loaded instance is expired, so re-read by id afterwards."""
        ...
