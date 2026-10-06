"""The `EraseHistoryEntry` use case: delete one entry of a signed-in user's tailoring history — rows,
then files (slice 2.3, technical plan §0.6, AC-14).

`EraseAccount`'s shape, one entry wide, and it lives in `retention` for the same reason: *"delete
what an owner has for X, rows committed, then files, reporting what could not be unlinked"* is the
shape this context already owns twice. An entry spans three contexts' tables — the run, its export
jobs, its posting when nothing else references it — so no single context's repository may delete it.

A retention use case composing a tailoring one (`GetTailoringRun`) is fine here: `application/` may
cross contexts, and `DeleteOwnAccount` → `EraseAccount` is the precedent. `domain/retention` may
not, which is why the port takes the run id as a bare `UUID` and `HistoryEntryInProgress` a string.

Flow: ``run = await get_tailoring_run(run_id, UserOwner(user_id))`` (the 404 collapse, inherited)
→ `queued` / `running` raises `HistoryEntryInProgress(run.status.value)` with **nothing deleted**
→ ``deleted = await entries.delete_history_entry(user_id, run_id.value)`` (rows, committed by the
bound adapter; `None` → `TailoringRunNotFound`, a concurrent deletion won and the loser unlinks
nothing) → ``files.delete(ref)`` for each of `deleted.export_files`, collecting the type name of any
`Exception` → `HistoryEntryErasureReport`.

**This layer does not log**: unlink failures are *returned* as exception type names, and the entry
point writes one line per element (1.6's R-3/R-4 fix, third use).
"""

from __future__ import annotations

from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.errors import HistoryEntryInProgress
from tailorcraft.domain.retention.ports import HistoryEntryDataPort
from tailorcraft.domain.retention.value_objects import HistoryEntryErasureReport
from tailorcraft.domain.shared.files import FileStorePort
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus


class EraseHistoryEntry:
    """Erase `user_id`'s history entry `run_id` and unlink its export files (see the module
    docstring for the flow and every refusal)."""

    def __init__(
        self,
        get_tailoring_run: GetTailoringRun,
        entries: HistoryEntryDataPort,
        files: FileStorePort,
    ) -> None:
        self._get_tailoring_run = get_tailoring_run
        self._entries = entries
        self._files = files

    async def __call__(self, user_id: UserId, run_id: TailoringRunId) -> HistoryEntryErasureReport:
        # The authorization and the 404 collapse, inherited: `UserNotFound` for an erased account,
        # `TailoringRunNotFound` (from `TailoringRunNotOwnedByUser`) for a run that is not this
        # user's — a guest's included.
        run = await self._get_tailoring_run(run_id, UserOwner(user_id))

        # A worker may be on it: deleting the row under it would throw away a paid call's result.
        # Refused with nothing touched; the run decides within seconds (H-42).
        if run.status in (TailoringRunStatus.QUEUED, TailoringRunStatus.RUNNING):
            raise HistoryEntryInProgress(run.status.value)

        # Rows first, committed by the bound adapter, before any unlink (ADR-0006 §2). The port
        # takes the bare UUID: `domain/retention` may not name another context's types.
        deleted = await self._entries.delete_history_entry(user_id, run_id.value)
        if deleted is None:
            # A concurrent deletion won between the read and the delete (H-44). Its unlinks are
            # its own; this loser touches nothing.
            raise TailoringRunNotFound(str(run_id))

        unlinked = 0
        failures: list[str] = []
        for ref in deleted.export_files:
            try:
                await self._files.delete(ref)
            except Exception as exc:
                # `Exception`, never `BaseException`: a cancellation must still cancel. Only the
                # class name is kept — a message can quote a path — and it is returned, not logged:
                # the rows are already gone, so this is an orphan for the sweep, never a reason to
                # fail the deletion.
                failures.append(type(exc).__name__)
            else:
                unlinked += 1

        return HistoryEntryErasureReport(
            export_jobs=deleted.export_jobs,
            files_unlinked=unlinked,
            unlink_failures=tuple(failures),
            posting_deleted=deleted.posting_deleted,
            # The run's card went with it (slice 3.1, AC-13): a bool, never a tracking type.
            tracked_application_deleted=deleted.tracked_application_deleted,
        )


__all__ = ["EraseHistoryEntry"]
