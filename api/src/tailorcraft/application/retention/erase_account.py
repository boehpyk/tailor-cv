"""The `EraseAccount` use case: delete a registered account and everything it owns — rows, then
files (slice 2.2, technical plan §0.4 and §0.5, AC-11).

The purge's sibling, on request rather than on a timer, and it lives in `retention` for that reason
(ADR-0018's shape: a policy, no aggregate, port methods, a report). It is called by two entry points
with two different authorities: `DeleteOwnAccount` (the user, after a password re-confirmation) and
the operator's `erase-account` CLI (no password — operator authority, and the only way out for a user
who has forgotten theirs).

**Rows first, committed, then files** (ADR-0006 §2). `files_of_account` collects every key before
anything is deleted — once the rows are gone the keys cannot be recovered — and each unlink happens
only after `delete_account` has returned, which the bound adapter makes durable.

**This layer does not log**, and that is why unlink failures are *returned*: `AccountErasureReport`
carries each failure's exception **type name**, and the entry point writes one line per element (1.6's
R-3/R-4 fix, reused). A failed unlink leaves an orphan for the operator's sweep; it never fails the
erasure, because the rows are already gone.

**No command dataclass**: one verified id, the purge's precedent for a one-value input.

**SKELETON step (T8).** `__init__` is real; `__call__`'s body lands in T10.
"""

from __future__ import annotations

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.ports import AccountDataPort
from tailorcraft.domain.retention.value_objects import AccountErasureReport
from tailorcraft.domain.shared.files import FileStorePort


class EraseAccount:
    """Erase `user_id`'s account and unlink every file it owned.

    Flow: ``keys = await accounts.files_of_account(user_id)`` (→ `AccountNotFound`) →
    ``await accounts.delete_account(user_id)`` (`False` → `AccountNotFound`: a concurrent erasure
    won, and the loser unlinks nothing) → ``files.delete(key)`` for each key, collecting the type
    name of any `Exception` → `AccountErasureReport`.
    """

    def __init__(self, accounts: AccountDataPort, files: FileStorePort) -> None:
        self._accounts = accounts
        self._files = files

    async def __call__(self, user_id: UserId) -> AccountErasureReport:
        raise NotImplementedError
