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
"""

from __future__ import annotations

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.retention.ports import AccountDataPort
from tailorcraft.domain.retention.value_objects import AccountErasureReport
from tailorcraft.domain.shared.files import FileStorePort


class EraseAccount:
    """Erase `user_id`'s account and unlink every file it owned.

    Flow: ``keys = await accounts.files_of_account(user_id)`` (→ `AccountNotFound`) →
    ``counts = await accounts.count_account(user_id)`` (`None` → `AccountNotFound`) →
    ``await accounts.delete_account(user_id)`` (`False` → `AccountNotFound`: a concurrent erasure
    won, and the loser unlinks nothing) → ``files.delete(key)`` for each key, collecting the type
    name of any `Exception` → `AccountErasureReport`.
    """

    def __init__(self, accounts: AccountDataPort, files: FileStorePort) -> None:
        self._accounts = accounts
        self._files = files

    async def __call__(self, user_id: UserId) -> AccountErasureReport:
        # Collected before anything is deleted: once the rows are gone, the keys cannot be
        # recovered, and a key nobody collected is a file nobody unlinks until the sweep.
        refs = tuple(await self._accounts.files_of_account(user_id))

        # The counts, read in the same unit of work before the delete (slice 2.3, AC-15): once the
        # rows are gone there is nothing left to count. `None` means a concurrent erasure already
        # took the account between the two reads.
        counts = await self._accounts.count_account(user_id)
        if counts is None:
            raise AccountNotFound(str(user_id))

        if not await self._accounts.delete_account(user_id):
            # A concurrent erasure won between the read and the delete (S-46). Its unlinks are its
            # own; this loser's snapshot is thrown away rather than acted on twice.
            raise AccountNotFound(str(user_id))

        unlinked = 0
        failures: list[str] = []
        for ref in refs:
            try:
                await self._files.delete(ref)
            except Exception as exc:
                # `Exception`, never `BaseException`: a cancellation must still cancel. The class
                # name is the only thing kept — a message can quote a path — and it is returned,
                # not logged: the rows are already gone, so this is an orphan for the sweep, never
                # a reason to fail the erasure.
                failures.append(type(exc).__name__)
            else:
                unlinked += 1

        return AccountErasureReport(
            base_cvs=counts.base_cvs,
            files_unlinked=unlinked,
            unlink_failures=tuple(failures),
            tailoring_runs=counts.tailoring_runs,
            job_postings=counts.job_postings,
            export_jobs=counts.export_jobs,
            # Every key this erasure tried — saved-CV files and derived export files — so
            # `files_unlinked` and `unlink_failures` read against it.
            files=len(refs),
            # The account's cards, counted before the delete like the rest (slice 3.1, AC-13).
            tracked_applications=counts.tracked_applications,
        )
