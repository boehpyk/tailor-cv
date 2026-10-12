"""The log lines an account erasure and a history-entry erasure write, shared by every HTTP route
that erases one (the history entry's since 4.3, §0.5: the user's route and the admin's).

`delete-account` (slice 2.2) and the admin erase (slice 4.2) write byte-identical lines, so the
operator's grep for `retention.account_erased` finds both. The CLI has its own copy in
`retention/erase_account_command.py` (same event names), because it is an entry point outside the
HTTP layer.
"""

from __future__ import annotations

from typing import Final

import structlog

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.value_objects import (
    AccountErasureReport,
    HistoryEntryErasureReport,
)
from tailorcraft.domain.tailoring.value_objects import TailoringRunId

log = structlog.get_logger(__name__)

EVENT_ACCOUNT_ERASED: Final = "retention.account_erased"
EVENT_ACCOUNT_FILE_UNLINK_FAILED: Final = "retention.account_file_unlink_failed"
EVENT_HISTORY_ENTRY_ERASED: Final = "retention.history_entry_erased"
EVENT_HISTORY_ENTRY_FILE_UNLINK_FAILED: Final = "retention.history_entry_file_unlink_failed"


def log_erasure(user_id: UserId, report: AccountErasureReport) -> None:
    """S-45: one `retention.account_erased` line with the counts, and one
    `retention.account_file_unlink_failed` per failed unlink — ids, counts and class names only.

    `EraseAccount` returns its failures rather than logging them (the application layer does not
    log); this is the entry point writing them down, exactly as the purge's task does with its
    `PurgeReport`. Each failure is an orphan file for the operator's sweep (R-5)."""
    log.info(
        EVENT_ACCOUNT_ERASED,
        user_id=str(user_id.value),
        base_cvs=report.base_cvs,
        # Slice 2.3 (H-52): an account is also its history. `files` is every key the erasure tried
        # to unlink — saved CVs' and derived export keys — so the two numbers below read against it.
        tailoring_runs=report.tailoring_runs,
        job_postings=report.job_postings,
        export_jobs=report.export_jobs,
        files=report.files,
        files_unlinked=report.files_unlinked,
        files_failed=len(report.unlink_failures),
        # Slice 3.1 (T-34): the account's cards, gone by the cascade. A count.
        tracked_applications=report.tracked_applications,
    )
    for error_type in report.unlink_failures:
        log.warning(
            EVENT_ACCOUNT_FILE_UNLINK_FAILED, user_id=str(user_id.value), error_type=error_type
        )


def log_history_entry_erasure(
    user_id: UserId, run_id: TailoringRunId, report: HistoryEntryErasureReport
) -> None:
    """One `retention.history_entry_erased` line with the counts, and one
    `retention.history_entry_file_unlink_failed` per failed unlink — ids, counts and class names,
    never a storage key or a path. `EraseHistoryEntry` returns its failures rather than logging them
    (the application layer does not log); `log_erasure` above is the same shape."""
    log.info(
        EVENT_HISTORY_ENTRY_ERASED,
        user_id=str(user_id.value),
        tailoring_run_id=str(run_id.value),
        export_jobs=report.export_jobs,
        files_unlinked=report.files_unlinked,
        files_failed=len(report.unlink_failures),
        posting_deleted=report.posting_deleted,
        # Slice 3.1 (T-32, AC-24): whether the entry's card went with it. A bool.
        tracked_application_deleted=report.tracked_application_deleted,
    )
    for error_type in report.unlink_failures:
        log.warning(
            EVENT_HISTORY_ENTRY_FILE_UNLINK_FAILED,
            tailoring_run_id=str(run_id.value),
            error_type=error_type,
        )
