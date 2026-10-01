"""Ports the `retention` context needs from the outside world, in the domain's own language.

Neither protocol below names a table, a column, a `SELECT`, a directory, a volume, `os.scandir`, a
cursor or a transaction — that is adapter business. What they name is what retention *asks*: how
much is overdue, give me the next batch with its file keys, delete this session, and which of these
keys does anything still point at (ADR-0002, AC-5).

**These get no red-first cycle, and the reason is stated rather than left to look like an exemption**
(docs/sdlc.md §2, and the identical paragraph in `domain/export/ports.py`, `domain/tailoring/ports.py`
and `domain/posting/ports.py`). A `Protocol` has no behaviour: every method body is `...`, so there
is nothing that could fail an assertion, and a "test" of one would either assert that Python still
has ellipses or silently test whichever adapter it imported to stand in. What proves these are right
is that the adapters satisfy them — each carries an `if TYPE_CHECKING:` structural-conformance
assertion that makes `mypy --strict` do the checking — and that the use cases compile against them.

Every method is `async`, including `count_expired`, which a health probe calls. Both adapters do I/O
on the event loop's behalf, and a synchronous one would put a directory walk or a database round trip
on the loop for every concurrent user — the failure mode this codebase treats as CRITICAL because it
presents as "the app is slow" and never as an error.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.retention.value_objects import (
    AccountCounts,
    DeletedHistoryEntry,
    ExpiringGuestSession,
    ScannedFile,
)
from tailorcraft.domain.shared.files import FileRef


class ExpiredGuestDataPort(Protocol):
    """Everything retention needs to ask the store of record about guest data that has aged out.

    **Not methods on `GuestSessionRepository`.** That port belongs to `identity`, speaks about one
    session at a time in the language of authentication, and three of its four methods are about
    resolving a cookie. "How much is overdue?" and "give me the next batch with their file keys" are
    a different consumer's questions, and ADR-0016 already declined to add speculative methods for
    this slice on exactly that ground: *1.6 adds the one it needs.*

    **No aggregate of `intake`, `posting`, `tailoring` or `export` appears in any signature here,
    and that is AC-1 and AC-16 rather than taste.** Those rows go by the database cascade; their
    files come back as derived `FileRef` keys. The day this port needs to import one of them, the
    cascade contract has broken and that is a design change to argue, not an import to add.
    """

    async def count_expired(self, as_of: datetime) -> int:
        """How many guest sessions are expired at `as_of` — the **backlog**, and the one signal an
        operator is told to trust (ADR-0018 decision 4).

        It is computed from the same predicate `list_expired` selects on, which is the whole point:
        a run row, a log line and a heartbeat can all be written by a job that is not working, while
        a count of what is still overdue cannot be faked by one. `/health/ready` publishes it.
        """
        ...

    async def list_expired(self, as_of: datetime, limit: int) -> Sequence[ExpiringGuestSession]:
        """The next batch of expired sessions, **oldest `expires_at` first**, at most `limit` of them.

        Oldest first is a privacy decision before it is an ordering one: the people whose data has
        been overdue longest are cleared first. It also makes a `--limit` run deterministic and
        therefore testable (AC-7) — an unordered batch would make "which two did it take?" a
        question with no right answer.

        Each element carries its file keys, **collected in the same read, before anything is
        deleted** (ADR-0006 §2, ADR-0018 decision 3): once the rows are gone the keys cannot be
        recovered, and an export's key may not even be in its row (a `rendering` or `failed` job can
        have written bytes while `file_key IS NULL`), so the adapter derives it from `(id, format)`
        through `FileRef.for_export`.
        """
        ...

    async def delete_session(self, session_id: GuestSessionId) -> bool:
        """Delete one guest session and everything the cascade takes with it; return `True` iff
        **this call's** `DELETE` removed the session row.

        **The answer is not a count, and that is why this reverses the old contract** (ADR-0018
        amendment (a), slice 2.4). The method used to return nothing, on the argument that "how many
        rows went" is a question about the cascade — the database's mechanism, not this port's
        promise — and a number the domain could only misuse. That argument still holds against a
        count; it never covered this. Since 2.4 a guest session can be **claimed**: its rows re-keyed
        to a user and the session row deleted, in one transaction. If a claim commits between
        `list_expired` (which collected this session's file keys, without a lock) and this call, the
        cascade here takes nothing, the rows are a user's now, and unlinking the collected keys would
        delete a registered user's files. Because the claim deletes the session row itself, *"my
        `DELETE` removed the row"* holds exactly when no claim committed first — and none can commit
        after, since the claim locks the row `FOR UPDATE` before re-keying and this `DELETE` holds it.
        So the boolean is the one fact that says whether the keys the caller holds still belong to
        what it deleted: **unlink only what you deleted.**

        Deleting an already-deleted session is still a **no-op, not an error** (AC-15, R-19) — it now
        also says so, by returning `False`. That is half of what makes the purge safe to retry,
        redeliver and run twice concurrently, and it means the loser of two concurrent purges skips
        the session rather than reporting it a second time.

        The adapter contains each delete in a SAVEPOINT and commits per session. That is not a
        detail the port states — it may not — but it is the reason this method takes one id and not
        a batch: a failed flush rolls back to the nearest transaction boundary and expires the whole
        identity map *inside* the flush, so a batch-wide delete would turn one bad row into
        `MissingGreenlet` on every remaining candidate (measured in 1.4).
        """
        ...

    async def which_are_referenced(self, keys: Sequence[FileRef]) -> frozenset[FileRef]:
        """Of these storage keys, which ones does a live row still point at (AC-23, R-33).

        The orphan sweep's cross-check, and the reason it is a **third method on this port rather
        than a fourth port** (OQ-4): it is a question about the same rows, answered by the same
        adapter over the same connection, and a port per query is ceremony. One batched read over
        `intake_base_cv.file_key` and `export_job.file_key`, not `len(keys)` round trips.

        It returns the **referenced** set rather than the orphaned one on purpose. The caller deletes
        what is *not* in the answer, so an adapter bug that returns too little can only spare files,
        never delete them — and if this raises at all, the sweep fails **closed** with
        `OrphanScanAborted` and unlinks nothing. Deleting a file because we could not ask whether it
        is referenced is the one irreversible mistake this tool can make (ADR-0018 decision 6).
        """
        ...


class OrphanFileScannerPort(Protocol):
    """Walk the file store and report what is older than a cutoff, in the domain's language.

    **A separate port, not a method on `FileStorePort`.** That one is `put`/`get`/`delete` — a
    key-value store. Listing is a capability exactly one consumer wants, and bolting it on would
    oblige a future S3 adapter to implement pagination on behalf of every caller of a port that never
    needed it. ADR-0011 §4's *"sweepable without the database"* is a property of the **layout**, and
    this port is the single place that knowledge is used.
    """

    async def scan_older_than(self, cutoff: datetime) -> Sequence[ScannedFile]:
        """Every entry in the store created at or before `cutoff`, as `ScannedFile` values.

        The walk is synchronous work on a real filesystem, so the adapter does it in a thread
        (AC-42) — a directory walk on the event loop blocks every concurrent user.

        An entry whose name is not a valid `FileRef` key comes back as `ref=None` and is never named
        (R-37): the port's return type is what makes an unrecognised filename — possibly a person's
        name — structurally unable to reach a log line. And `cutoff` being the caller's business, not
        this adapter's, is what keeps the window-plus-grace floor a single decision in the use case
        rather than a constant hidden in a directory walk.
        """
        ...


class AccountDataPort(Protocol):
    """Everything retention needs to erase one registered account: the purge's sibling, on request
    rather than on a timer (technical-plan §0.5, ADR-0006 amendment).

    **Not methods on `UserRepository`**, for the reason `ExpiredGuestDataPort` is not on
    `GuestSessionRepository`: identity's port speaks the language of authentication, one user at a
    time, and "which files does everything this account owns name?" is retention's question.

    The order is the purge's and it is the caller's to keep: `files_of_account` **before**
    `delete_account` (once the rows are gone the keys cannot be recovered), and every unlink **after**
    `delete_account` has returned (the survivor of a crash is an orphan file for the sweep, never a
    row pointing at nothing — ADR-0006 §2). No aggregate appears in any signature: the account's rows
    go by the database cascade and come back here only as `FileRef` keys and counts.
    """

    async def files_of_account(self, user_id: UserId) -> Sequence[FileRef]:
        """The storage keys of every file the account's rows name, for the unlinks that follow.

        Since slice 2.3 that is the saved base CVs' keys **and** the account's export files, each
        export key **derived** from the job's `(id, format)` for a queued format — never read from
        its `file_key` column — so a `rendering` or `failed` job's already-written bytes are not
        missed (the purge's rule, ADR-0018 decision 3; AC-15).

        Raises `AccountNotFound` if there is no such user. The answer must be **complete for a
        `delete_account` of the same user in the same unit of work**: a row inserted between this
        read and that delete would be cascaded away with its file never collected. How the adapter
        keeps that promise (it locks the user row) is adapter business, not this port's — a
        transaction is not a word in retention's language.
        """
        ...

    async def delete_account(self, user_id: UserId) -> bool:
        """Delete the user and everything the cascade takes with it (logins, retired refresh
        tokens, saved base CVs — and since slice 2.3 their tailoring runs, job postings and export
        jobs). Rows only — never a file.

        Returns `False` if nothing was deleted (a concurrent erasure won), `True` otherwise. The
        deletion is durable when this returns: the caller unlinks files next, and "rows first" is a
        statement about durability that only a committed delete makes true.
        """
        ...

    async def count_account(self, user_id: UserId) -> AccountCounts | None:
        """What `delete_account` *would* take, for `erase-account --dry-run` (AC-31; widened in
        slice 2.3 to runs, postings, export jobs and CV-plus-export files, AC-36). Deletes
        nothing. `None` if there is no such user — the dry run's answer to "not found" is not an
        exception, because reporting on an absent account is an ordinary outcome of looking."""
        ...


class HistoryEntryDataPort(Protocol):
    """Everything retention needs to delete one entry of a signed-in user's history: the run, its
    export jobs, and its posting if nothing else references it (slice 2.3, technical plan §0.6).

    **Not a `remove` on `TailoringRunRepository`**: an entry spans three contexts' tables, and a
    tailoring repository deleting export and posting rows would be one context reaching into two
    others. "Delete what an owner has for X, rows committed, then files" is retention's shape
    already (the purge, account erasure), so the port lives here.

    **`run_id` is a bare `UUID`, not `TailoringRunId`, on purpose.** `domain/retention` may import
    only `shared` and `identity` (1.6's AC-1 allowlist, `tests/unit/retention/test_import_allowlist.py`):
    retention reaches other contexts' rows through its own ports and never through their types. The
    application layer, which may cross contexts, passes `run_id.value` after `GetTailoringRun` has
    authorized it. A reader who expects the typed id here should find this paragraph rather than
    "fix" the import.
    """

    async def delete_history_entry(
        self, user_id: UserId, run_id: UUID
    ) -> DeletedHistoryEntry | None:
        """Delete the run, its export jobs and — when no other run references it — its posting, all
        owned by `user_id`, in one transaction. Rows only — never a file.

        Durable on return: the bound adapter commits, because the caller unlinks the returned
        `export_files` next and "rows first" is a statement about durability. `None` when the run
        row was not there (a concurrent deletion won): nothing else is touched, and the loser
        unlinks nothing (H-44).
        """
        ...
