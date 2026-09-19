"""The `PurgeExpiredGuestSessions` use case: delete the guest data that has outlived its window.

Read it next to `application/export/abandon_stale_export_jobs.py`. The two share a shape — a
bounded, ordered, per-tick batch with a tolerated per-item failure and no command dataclass — and
they are deliberately not generalized over, for the reason that module already argues at length:
shared shape is not shared behaviour. What differs here is the only thing that matters. That sweep
records a row; **this one deletes rows and unlinks files**, which is irreversible, so every choice
below is made on the side whose loss is recoverable.

**SKELETON step.** `__init__` is fully written and really stores its arguments, so `qa`'s T9 RED
tests fail on their *assertions* rather than on a `TypeError` from the constructor or an
`ImportError` from the module — an `ImportError` red proves a file is absent, not that the assertion
discriminates (docs/sdlc.md §2). Only `__call__`'s body is deferred; its signature and return type
are real, and T10 fills in the five steps its docstring specifies.

**This layer does not log** — the house rule `application/export/render_export_job.py`'s module
comment writes out in full. This context has no event to publish either (ADR-0018 decision 9: a
`GuestDataPurged` payload would reach every listener and every log line, the one place Constitution
§8 says a CV-adjacent value must never go), so the application layer's channel here is the returned
`PurgeReport` and nothing else. The per-run line — `sessions_deleted`, `files_unlinked`,
`duration_ms`, `dry_run` — belongs to the entry point, the CLI or the Celery task, built from that
report. A second logging channel opened here would emit records the privacy test cannot see.
"""

from __future__ import annotations

from tailorcraft.domain.retention.ports import ExpiredGuestDataPort
from tailorcraft.domain.retention.value_objects import PurgeReport, RetentionWindow
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.files import FileStorePort


class PurgeExpiredGuestSessions:
    """Delete every guest session whose window has closed, and unlink the files it put on disk — a
    bounded batch per run, oldest expiry first (FR-6, ADR-0006, ADR-0018).

    **No command dataclass, and the absence is deliberate**, exactly as in `AbandonStaleExportJobs`
    and `AbandonStaleTailoringRuns`. Every use case that takes a frozen input dataclass acts on
    behalf of a caller who names what to act on. This one names nothing: it acts on whatever is
    expired *now*, and its three bounds — the window, the batch size and the dry-run flag — are
    configuration fixed at construction. A zero-field command would be a contract with nothing in it.

    Both entry points construct it the same way: Celery beat on the default queue (ADR-0018 decision
    9) and the operator's CLI behind `make purge`. The difference between them is which arguments
    the composition root passes, not which code path runs — `--dry-run` and `--limit` are
    constructor arguments, so there is exactly one implementation of what a purge *is*.

    Flow (technical plan §2, "Use case 1"; T10 implements it) — see `__call__`.

    **Rows first, committed, then files — per session, not per batch** (ADR-0006 §2, ADR-0018
    decision 2). The order is not a preference; it is a choice between two survivors. Unlink first
    and a crash leaves a **row pointing at nothing**, which reaches a user as a download that 404s
    from a UI still offering it — a defect with a victim and no recovery. Delete first and a crash
    leaves a **file nobody references**, which costs disk and is swept up by `ReclaimOrphanedFiles`
    in this same slice. So the recoverable loss goes on the file side, every time.

    **The use case names no transaction, and it may not** (ADR-0002 — `application/` imports
    `domain` only, so there is no session, no `commit()` and no SAVEPOINT anywhere in this module).
    "Committed" in the sentence above is therefore a promise the **committing adapter** keeps: the
    composition root wraps `ExpiredGuestDataPort` so that each `delete_session` is its own committed
    transaction, exactly as `CommittingExportJobRepository` does per write one context along. This
    class's contribution is the *ordering* — it does not call `files.delete` for a session until
    `delete_session` has returned — and the adapter's contribution is making that return mean
    durable.

    **Why a single transaction over the batch would be wrong**, since it is the obvious simplifying
    edit. It makes a mid-batch failure **un-partial**: one refused `DELETE` at candidate 40 rolls
    back the 39 sessions already deleted, so the run reports work it then undid, and the backlog does
    not move however often the job runs. Worse, it inverts the crash window this ADR just chose —
    the files of those 39 sessions were **already unlinked**, against rows that have now come back.
    That is precisely the "row pointing at nothing" outcome the ordering exists to prevent, delivered
    by the mechanism meant to make things safer. A purge is a batch that must survive one bad row,
    not a batch that must be all-or-nothing.

    **Why the per-session SAVEPOINT is not optional** (AC-13, and CLAUDE.md's expired-identity-map
    lesson, measured at 1.4's verify). A failed flush rolls back to the nearest transaction boundary,
    and at the root that boundary is `_restore_snapshot(dirty_only=False)`: **every loaded instance
    is expired inside the flush**, before the `except` clause below it ever runs. The next attribute
    read on any remaining candidate is then a lazy load, which on an `AsyncSession` is
    `MissingGreenlet` — so one refused `DELETE` would not cost one session, it would cost every
    session after it in the batch, with an error naming greenlets rather than rows. The adapter
    therefore contains each delete in `begin_nested()`; and because `begin_nested()` **flushes on
    entry**, nothing dirty may be handed to it (`expunge` → `begin_nested()` → flush → commit).

    **No `except Exception` floor, and this is deliberate** (R-15, `domain/retention/errors.py`).
    It contradicts a rule that holds two modules away, so it is worth stating rather than leaving to
    look like an oversight: a **port** promises to translate every failure into the domain's language
    and therefore needs a catch-all underneath its named translations (ADR-0012 obligation 10, which
    `LocalFileStore` implements). A **use case promises no such thing.** A floor here would convert a
    bug in a job that *deletes things* into a green exit code and a tidy-looking report of zeroes —
    the single worst failure mode this feature has, because `overdue` would keep climbing behind a
    run that says `ok`. Exactly two failures are caught, both named, both per-item, both with a
    count in the report that makes them visible: a refused `delete_session` (R-3) and a
    `FileStoreUnavailable` on an unlink (R-4). Everything else escapes, fails the task or the CLI,
    and leaves whatever had already committed committed (R-1).

    **Idempotency** (AC-15, R-19). Three separate facts make a retried tick, a redelivered message
    and two concurrent runs all safe, and none of them is a lock:

    - a deleted session is no longer expired-and-present, so `list_expired` never returns it twice;
    - a second `DELETE` on an already-deleted session affects zero rows and is a **no-op, not an
      error**, by `ExpiredGuestDataPort.delete_session`'s contract;
    - `FileStorePort.delete` is `missing_ok` by contract, so an already-unlinked key is not an error
      either.

    The Redis lock in the composition root is an operational nicety on top of that, which is why it
    is allowed to **fail open** (ADR-0018 decision 6): the cost of an overlap is duplicated work on
    an idempotent job, and the cost of a skipped purge is a broken privacy promise.

    **It never loads an aggregate of another context** (AC-16). `ExpiringGuestSession` carries an id,
    an instant and a tuple of `FileRef` — there is nowhere in the values this class touches to put a
    CV, a cover letter, an `original_filename` or a path, so no entry point downstream can leak one
    even by accident. The other contexts' rows go by the database cascade and their files by a key
    **derived** from `(id, format)`, which is ADR-0016's determinism guarantee being consumed for the
    first time (AC-9).
    """

    def __init__(
        self,
        data: ExpiredGuestDataPort,
        files: FileStorePort,
        clock: Clock,
        window: RetentionWindow,
        batch_limit: int = 100,
        dry_run: bool = False,
    ) -> None:
        self._data = data
        self._files = files
        self._clock = clock
        self._window = window
        self._batch_limit = batch_limit
        self._dry_run = dry_run

    async def __call__(self) -> PurgeReport:
        """Run one purge and report it in counts. T10 implements these five steps.

        1. ``now = self._clock.now()`` — **one instant for the whole run** (AC-6), never
           `datetime.now()`. The listing, every `expires_at` comparison and the instant the entry
           point writes to the heartbeat are then the same instant, so a run cannot disagree with
           itself about when "now" was. ``cutoff = self._window.expiry_cutoff(now)`` — and note that
           `expiry_cutoff` returns `now` unchanged *on purpose*: the window was applied once already,
           at `GuestSession.start`, and subtracting it again here would silently turn a 24-hour
           retention promise into a 48-hour one. Its docstring spells that out; do not "fix" it.

        2. ``candidates = await self._data.list_expired(as_of=cutoff, limit=self._batch_limit)`` —
           **oldest `expires_at` first and bounded**, by the port's contract (AC-7). Oldest first is
           a privacy decision before it is an ordering one: the people whose data has been overdue
           longest are cleared first, and a `--limit` run becomes deterministic and therefore
           testable. Each element already carries its file keys, collected **in that same read,
           before anything is deleted** (ADR-0018 decision 3) — after the rows go, the keys are
           unrecoverable, and an export job in `rendering` can hold bytes while its `file_key IS
           NULL`, so its key is derived rather than read (AC-9).

        3. **If ``self._dry_run``, return here and write nothing of any kind** (R-13, AC-17). Not a
           `delete_session`, not a `files.delete`, not a heartbeat, not a lock — the report is the
           only output, and the *absence* of side effects is the acceptance criterion, not a
           nice-to-have. The report is ``examined=len(candidates)``, every deletion count `0`, and
           ``files_unlinked`` = the total number of keys across all candidates, i.e. what the run
           *would* have unlinked; the entry point is what words it as "would unlink" and prints the
           literal `Nothing was deleted.` ``dry_run=True`` is a **field** rather than something the
           caller is trusted to remember, so this report can never be read as a record of deletions
           that never happened.

        4. Otherwise, **per candidate, in order** — the order matters twice over, once for the
           batch (oldest first) and once within each candidate (row, then its files):

           - ``await self._data.delete_session(c.session_id)``. On failure: count `sessions_failed`,
             **do not unlink that session's files**, and continue to the next candidate (R-3,
             AC-13). Not unlinking is the rows-first rule holding under failure: the session's rows
             are still there and still name those files, so removing the bytes would manufacture
             exactly the broken-download state the ordering exists to prevent. The batch continues
             because the next candidate is still overdue and its privacy promise is still unkept.
           - **Only then**, for each ``ref`` in ``c.files``: ``await self._files.delete(ref)``. On
             `FileStoreUnavailable` (`domain.shared.files`), count `files_failed` and continue
             (R-4, AC-14). The row is **already deleted and committed** — that is the crash window
             ADR-0006 §2 chose deliberately, and the survivor is an orphan file, which
             `ReclaimOrphanedFiles` reclaims. Nothing is rolled back and no exception is re-raised:
             a full volume must not stop a purge from deleting rows.
           - Count `sessions_deleted` once, and `files_unlinked` once per key handed to the store.
             `files_unlinked` means *keys we asked the store to remove* and does **not** claim the
             file existed (R-5) — `FileStorePort.delete` is `missing_ok` by contract, so nobody here
             can compute the honest version of that number and an honest name is better than a
             precise-sounding wrong one.

        5. Return the `PurgeReport`, with ``duration_ms`` measured over the **whole run** using
           `time.perf_counter`, not the `Clock`: the clock port is whole-second by contract
           (ADR-0007) and cannot measure a sub-second batch at all — 1.3's `llm_duration_ms`
           argument and 1.5's `render_duration_ms` argument, unchanged.

        Nothing else is caught. An unexpected exception escapes this method by design (R-15): the
        task or the CLI fails, whatever had committed stays committed, and the **backlog** — not a
        log line and not a heartbeat — is what tells the operator how much is still overdue
        (ADR-0018 decision 4). The next run is the retry, and it is safe because the run is
        idempotent.
        """
        raise NotImplementedError
