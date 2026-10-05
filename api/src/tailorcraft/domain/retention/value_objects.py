"""Value objects for the `retention` bounded context: the window, one expired session, two reports.

**SKELETON step.** The field declarations below are real — a dataclass field *is* its contract, and
`qa`'s RED test (T4) has to be able to construct these types and read their annotations or it fails
on a `TypeError` from the stub rather than on the assertion it was written for. What is deferred is
*logic*: `RetentionWindow.__post_init__` and `RetentionWindow.expiry_cutoff` raise
`NotImplementedError` until the GREEN step fills them in (docs/sdlc.md §2).

The theme running through this module is that **the type is the privacy control**. Everything the
purge and the orphan sweep hand back upward is ids, instants, counts, flags and — since 1.6's
`/verify` — the *class name* of an exception: there is nowhere in any of these shapes to put a CV, a
cover letter, an `original_filename` or a filesystem path, so no caller downstream — no log line, no
health payload, no Sentry frame — can leak one by accident (AC-4, AC-16, Constitution §8).

`SessionPurgeFailure` and `FileUnlinkFailure` are that theme applied to the one thing this context
could not say before: *which* session refused to go, and *what kind* of error refused it. They are
the reason `PurgeReport` grew two fields, which contradicts AC-4's letter on purpose — the amendment,
and the argument that the intent is unharmed, is in `docs/specs/retention-guest-purge/feature-spec.md`
under AC-4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable


@dataclass(frozen=True, slots=True)
class RetentionWindow:
    """How long guest-owned data may live, as a type rather than an `int` (FR-6, ADR-0006 §1).

    Three readers share this one promise — the purge's cutoff, `StartGuestSession`'s `expires_at`
    and the session cookie's `Max-Age` — and a bare `int` passed between them is precisely how three
    readers of one promise drift apart. It wraps `settings.guest_retention_hours` at the composition
    root and nowhere else, because `os.environ` is read in exactly one place.

    *(It does **not** replace `StartGuestSession`'s `retention_hours: int` parameter in this slice.
    Putting one type on both ends of the promise is the obvious follow-up and it edits `identity`
    for a tidiness gain, which this slice does not need — OQ-7, ADR-0018.)*
    """

    hours: int

    def __post_init__(self) -> None:
        """Refuse a non-positive window (AC-3).

        Zero or negative hours is not a shorter retention policy, it is a rule that expires a
        session at or before the instant it was created — a configuration typo that would present as
        the purge deleting live guests' work seconds after they uploaded it. `InvariantViolated`
        (`domain.shared.errors`) is the error; it is the one an unconstructable state gets
        everywhere else in this codebase.
        """
        if self.hours <= 0:
            raise InvariantViolated("hours must be > 0 (a retention window is a positive span)")

    def expiry_cutoff(self, now: datetime) -> datetime:
        """The instant a session's `expires_at` must be at or before for it to count as expired.

        `now` arrives from the `Clock` port — **never** `datetime.now()`, which is what makes every
        rule in here testable without sleeping or freezing the process (AC-3, `domain/shared/clock.py`).
        The caller passes one instant for a whole run (AC-6), so a run cannot disagree with itself
        about when "now" was.

        The comparison this feeds is **inclusive** at the boundary: a session whose `expires_at` is
        exactly the cutoff is expired and is selected (R-18), matching `GuestSession.is_expired`'s
        own `at >= expires_at` rather than inventing a second, subtly different rule one layer up.

        **This returns `now` unchanged, and ignores `self.hours` on purpose. Do not "fix" it.**
        The window is applied exactly *once*, at `GuestSession.start`, which freezes it into that
        session's `expires_at`. By the time the purge runs, the question is already
        `expires_at <= now`; there is nothing left to subtract, and subtracting here would apply the
        same window a second time. The plausible-looking edit —
        `return now - timedelta(hours=self.hours)` — turns a 24-hour retention promise into a
        48-hour one, and **nothing would look broken**: the job still runs on schedule, still logs,
        still deletes sessions, just a day later than the UI promises. No error, no alert, no
        symptom — only the privacy commitment in FR-6 quietly ceasing to be true. That is why the
        contradiction is spelled out here rather than left for a reader to rediscover.

        Three tests in `tests/unit/retention/test_value_objects.py` go red if anyone tries:
        `test_expiry_cutoff_returns_the_instant_handed_to_it_regardless_of_the_windows_hours`,
        parametrized `1h`, `24h` and `999h` over one identical `now` — three different windows must
        produce the identical cutoff, which is a property no subtraction can satisfy.

        *Then why does `hours` exist at all, if this method never reads it?* For three jobs that are
        not this one. It is the **validated** window — `__post_init__` is the only place a
        non-positive retention policy is refused. It is the floor the orphan sweep computes from:
        `ReclaimOrphanedFiles` subtracts `hours + grace` from `now` itself, in the application layer
        (technical plan §2), because that sweep genuinely is asking "how old is old enough". And it
        is the one type three readers of a single promise will eventually share — the purge's
        cutoff, `StartGuestSession`'s `expires_at` and the session cookie's `Max-Age` (OQ-7). A bare
        `int` handed between those three is exactly how they drift out of agreement.
        """
        return now


@dataclass(frozen=True, slots=True)
class ExpiringGuestSession:
    """One expired session and the keys of everything it put on disk, collected **before** anything
    is deleted (ADR-0006 §2, ADR-0018 decision 3 — after the row goes, the keys are unrecoverable).

    Three fields, and the third field's *type* is why this is a value object and not a row: `files`
    is a tuple of `FileRef`, an opaque storage key with a grammar (ADR-0011), so there is nowhere
    here to put a path, a filename or a byte of a CV (AC-4, AC-16). The purge therefore cannot log
    one even if a future entry point tries.

    It carries an id and not a `GuestSession`. The purge never loads the aggregate — it deletes by
    id and lets the cascade do the rest — so naming the aggregate type here would put a class in a
    signature that nothing in this context ever constructs.
    """

    session_id: GuestSessionId
    expires_at: datetime
    files: tuple[FileRef, ...]


@dataclass(frozen=True, slots=True)
class SessionPurgeFailure:
    """One session the purge could not delete, in the only two facts an operator can act on: which
    session it was, and what *kind* of error refused it (R-3).

    **`error_type` is a class name by convention, held at one construction site.** Every production
    caller builds this through `from_exception`, which reads `type(exc).__name__` and has no way to
    reach `str(exc)` or `exc_info`. The dataclass constructor is public, though, and the unit tests
    use it directly — so this is a convention the call sites keep, **not a property the type
    enforces**, and it is named that way here rather than over-claimed. Validating the string in
    `__post_init__` was considered and rejected: it would raise from inside a failure handler whose
    whole job is tolerance, turning one refused row into a failed run. The asymmetry runs the other
    way — a strange class name in a log line costs nothing; an aborted purge breaks a privacy
    promise. A reviewer watching this type is therefore the control, which is what makes the
    argument below worth stating rather than assuming. `str(exc)` on a refused `DELETE` carries
    the row it refused out through three layers
    — SQLAlchemy's `[parameters: …]` line, asyncpg quoting the values it sent, and PostgreSQL's
    `DETAIL: Failing row contains (…)` — and the row on the other end of this cascade belongs to
    somebody's CV. A field that *could* hold a message is a field somebody fills with one at 2 a.m.;
    a constructor that reads `type(exc).__name__` cannot (Constitution §8, AC-38).

    **The id is deliberately here, and it is the one identifier AC-38 permits.** A `GuestSessionId`
    is an opaque UUIDv7 this system generated: it names a row, not a person, and it is this
    codebase's correlation handle everywhere else. Without it the entry point's line could only say
    "a session failed", which is the silence R-3 exists to break — the operator's next move is to go
    and look at *that* session, and a count cannot tell them which one.

    Nothing else may join these two fields. Not `expires_at`, not the file keys, not a count of them:
    a `FileRef` is a storage key, and AC-4's rule — the part of it that did not change — is that no
    field of what the purge hands upward can carry a key, a path, a filename, or text a user wrote.
    """

    session_id: GuestSessionId
    error_type: str

    @classmethod
    def from_exception(cls, session_id: GuestSessionId, exc: Exception) -> SessionPurgeFailure:
        """Record a refused `DELETE` as its exception's class name, and never as anything else.

        `Exception`, never `BaseException`, and the annotation is doing work rather than decorating:
        it mirrors the catch site exactly, so a handler that tried to record a cancellation would
        not type-check. A cancelled purge must still cancel.
        """
        return cls(session_id=session_id, error_type=type(exc).__name__)


@dataclass(frozen=True, slots=True)
class FileUnlinkFailure:
    """One key the purge asked the store to remove and the store refused (R-4).

    **One field, and no `FileRef` anywhere near it.** The key is precisely what must not travel —
    it is the file's name on the volume, and R-4's row says it in so many words: *never the key,
    never the path*. The adapter has already logged the `errno` at the point where it still knew
    one; what reaches the entry point from here is that an unlink failed, and which kind of refusal
    it was.

    That the kind is worth carrying at all — rather than leaving this as the `files_failed` count —
    is `StoredFileMissing`'s doing. It is a **subclass** of `FileStoreUnavailable`, so this field
    separates "the volume is unwritable" from "the key resolved to nothing", and the second one is
    worth a name because a purge should never see it: `FileStorePort.delete` is `missing_ok` by
    contract, so a `StoredFileMissing` on this path means the store's own promise has broken rather
    than the disk being full.

    **No session id**, although the row above carries one. R-4's contract names `error_type` and the
    adapter's `errno` line and nothing else, and the counterpart id is a field this slice would be
    adding to a failure contract nobody has re-argued. If an operator ever needs to correlate an
    unlink failure to its session, that is an amendment to R-4 that says which won — not a field that
    appears quietly because it looked useful.
    """

    error_type: str

    @classmethod
    def from_exception(cls, exc: FileStoreUnavailable) -> FileUnlinkFailure:
        """Record a refused unlink as its exception's class name.

        The parameter is typed to the one error `FileStorePort.delete` documents, which keeps this
        as narrow as the catch it mirrors (R-4) — the asymmetry with `SessionPurgeFailure`'s broad
        `Exception` is forced by the ports' contracts, not by taste, and the use case's catch sites
        explain it at length.
        """
        return cls(error_type=type(exc).__name__)


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """What one purge run did, in counts (AC-4).

    **No defaults on any field, and the field set is pinned by a test.** A default on a count is how
    a second construction site forgets one and reports a confident zero — the lesson 1.4 recorded on
    `conflicts`. And because AC-4's test asserts this field set exactly, widening it is a deliberate
    edit a reviewer sees, which is the guard against someone one day adding the "helpful" field that
    carries a filename.

    `files_unlinked` means *keys we asked the store to remove*. `FileStorePort.delete` is
    `missing_ok` by contract, so this number does **not** claim the file existed (R-5) — an honest
    name for a number nobody can compute is better than a precise-sounding one that is wrong.

    `sessions_failed` and `files_failed` exist because the run is deliberately partial: one refused
    `DELETE` neither aborts the batch nor unlinks that session's files (R-3, R-4, AC-13, AC-14).
    A run that touched a bad row is a run with a non-zero count, not a run that did not happen.

    **`session_purge_failures` and `file_unlink_failures` are the detail behind those two counts, and
    they are why AC-4 was amended rather than obeyed** (see the amendment in the feature spec, and
    the two value objects above). 1.6's `/verify` found that R-3's `retention.session_purge_failed`
    and R-4's `retention.file_unlink_failed` existed in the failure contract and nowhere in
    `api/src/`, which made a permanently refused `DELETE` invisible: the CLI's batch loop stops as
    soon as a batch deletes nothing, the run exits 0, `overdue` sits above zero for ever, and no line
    anywhere names the session or the reason. That is the *"job whose failure mode is silence"* this
    whole slice exists to defeat, reappearing one level down. The counts could only say *how many*;
    the entry point needs *which* and *what kind* to emit R-3's and R-4's lines, and it cannot ask
    the use case afterwards because the exception is long gone by then.

    **This layer still does not log** — the two tuples are the return channel, and the entry point
    (the Celery task, the CLI) is what turns each element into a line. That boundary is the reason
    the widening is safe to make twice over: an application layer that had started logging would
    emit records the AC-38 privacy test cannot see.

    **The counts are derived from the tuples at the one construction site, and `__post_init__`
    refuses a report where they disagree.** One fact, two representations, is exactly the shape 1.5
    recorded as a second derivation that can drift from its source; here it cannot, because the
    disagreement is unconstructable.

    `dry_run` is a field rather than something the caller remembers, so a report can never be read as
    a record of deletions that a dry run did not perform (R-13).

    **`sessions_skipped`** (ADR-0018 amendment (a), slice 2.4) counts sessions whose
    `delete_session` returned `False`: the row was already gone — claimed by a user between the read
    and the delete, or taken by a concurrent purge — so their collected keys were **not** unlinked.
    It exists so an operator can see the claim/purge race happen; it should read approximately zero.
    Like every other count here it has **no default**, even though one would have spared the
    construction sites an edit: a defaulted count is exactly the confident zero this docstring's
    first paragraph refuses.

    In a run that deleted, `sessions_deleted + sessions_skipped + sessions_failed == examined`:
    every candidate examined ends in exactly one of the three. A dry run deletes nothing, so all
    three are zero. `__post_init__` refuses any other shape, which is what makes a construction site
    that forgets to count a skip (and so counts it nowhere) unconstructable.
    """

    examined: int
    sessions_deleted: int
    sessions_skipped: int
    sessions_failed: int
    session_purge_failures: tuple[SessionPurgeFailure, ...]
    files_unlinked: int
    files_failed: int
    file_unlink_failures: tuple[FileUnlinkFailure, ...]
    duration_ms: int
    dry_run: bool

    def __post_init__(self) -> None:
        """Refuse a report whose counts and detail disagree.

        `sessions_failed` and `len(session_purge_failures)` are one fact written twice, and a fact
        written twice is a fact that can drift — 1.5's lesson about a derived copy that goes on
        disagreeing with the resource it was derived from. The use case derives both counts from the
        tuples it just built, so this can only fire against a future second construction site that
        supplied one without the other, which is precisely the site worth stopping. `InvariantViolated`
        (`domain.shared.errors`) is what an unconstructable state gets everywhere else here.

        **Neither message names a session or a key** — only the two numbers that disagreed. An
        invariant's error text ends up in a log and a Sentry frame like any other string.
        """
        if self.sessions_failed != len(self.session_purge_failures):
            raise InvariantViolated(
                "sessions_failed must equal len(session_purge_failures) "
                f"({self.sessions_failed} != {len(self.session_purge_failures)})"
            )
        if self.files_failed != len(self.file_unlink_failures):
            raise InvariantViolated(
                "files_failed must equal len(file_unlink_failures) "
                f"({self.files_failed} != {len(self.file_unlink_failures)})"
            )
        if self.sessions_skipped < 0:
            raise InvariantViolated(f"sessions_skipped must be >= 0 ({self.sessions_skipped})")
        # The partition of `examined` (ADR-0018 amendment (a)). Checked after the two count/detail
        # invariants above so that a report wrong in both ways names the more specific fault first.
        accounted = self.sessions_deleted + self.sessions_skipped + self.sessions_failed
        expected = 0 if self.dry_run else self.examined
        if accounted != expected:
            raise InvariantViolated(
                "sessions_deleted + sessions_skipped + sessions_failed must equal "
                f"{'0 in a dry run' if self.dry_run else 'examined'} ({accounted} != {expected})"
            )


@dataclass(frozen=True, slots=True)
class OrphanScanReport:
    """What one orphan sweep did, in counts — the same rules as `PurgeReport`: no defaults, exact
    field set, nothing that can carry a name.

    The four "did not touch it" counts are the point of the report rather than noise. `referenced`
    is the cross-check doing its job; `too_young` is the window-plus-grace floor holding a file that
    may still belong to a live session; `unrecognized` is a file whose name is not a `FileRef` key
    (R-37) — **counted, never named and never deleted**, because the one thing on that volume that
    could be a person's name is a filename nobody in this system wrote. A non-zero `unrecognized` is
    worth a human look, which is exactly what a count can prompt and a deletion cannot undo.
    """

    scanned: int
    referenced: int
    too_young: int
    unrecognized: int
    reclaimed: int
    failed: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """One file the orphan scanner found on the volume, in the domain's language.

    **There is no name field, and its absence is the control** (R-37). An unrecognised filename on
    the uploads volume may be a person's name — someone dropped a CV in by hand, or a stray
    `.DS_Store` sits beside one — and a string field here would make that name loggable, Sentry-able
    and reportable from four call sites that each look harmless. So an unrecognised file leaves the
    adapter as `ref=None`, and the use case can do exactly two things with it: count it, and leave
    it alone. Making the leak *unrepresentable* beats remembering not to print it, the same way
    `FileRef`'s grammar makes a traversing key unconstructable rather than rejected later.

    `created_at` comes from the filesystem, not from the id: `is_partial` files and unrecognised
    ones have no UUIDv7 to read an instant out of, and the sweep's age floor must judge every entry
    it found by one rule.

    `is_partial` marks a `.part` file (ADR-0011 §5). Nothing ever references one, so an old `.part`
    is reclaimable without asking the database at all — which is why it is a flag on the element
    rather than a second port method.
    """

    ref: FileRef | None
    created_at: datetime
    is_partial: bool


# --------------------------------------------------------------------------------------------------
# Slice 2.2 — account erasure (ADR-0006 amendment).
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountErasureReport:
    """What erasing one registered account did (AC-11): counts, and the exception **type names** of
    the unlinks the store refused.

    The purge's rules, reused rather than re-argued: no defaults, nothing that can carry a key, a
    path, a filename or a message. `unlink_failures` is returned rather than logged — the use case
    does not log, and the entry point turns each element into one line (1.6's R-3/R-4 fix). The rows
    are already gone and committed by the time any unlink is tried, so a failure here is an orphan
    for the operator's sweep, never a reason to fail the erasure.
    """

    base_cvs: int
    files_unlinked: int
    unlink_failures: tuple[str, ...]
    # Slice 2.3 (AC-15): the account's history is erased with it. `files` is every key the erasure
    # tried to unlink — saved-CV files **and** derived export files — so `files_unlinked` and
    # `unlink_failures` can be read against it. No defaults, like every other field here.
    tailoring_runs: int
    job_postings: int
    export_jobs: int
    files: int
    # Slice 3.1 (T7). A deliberate exception to this class's "no defaults" rule: the default lets the
    # field land before anything fills it, so every existing report stays valid and behaviour is
    # unchanged. The adapter fills it from T17, and `EraseAccount` passes it through from T9.
    # The account's tracked applications (cards) the erasure deleted. A count, never a tracking type.
    tracked_applications: int = 0


@dataclass(frozen=True, slots=True)
class AccountCounts:
    """What erasing one account *would* delete, for `erase-account --dry-run` (AC-31): the saved
    base CVs, the stored files, and the logins — and since slice 2.3 (AC-36) the tailoring runs, the
    job postings and the export jobs. Counts only.

    `files` is **widened in meaning, not added**: since 2.3 it counts saved-CV files and the
    account's derived export files together (AC-15), exactly as `AccountDataPort.files_of_account`
    will return both.
    """

    base_cvs: int
    files: int
    logins: int
    tailoring_runs: int
    job_postings: int
    export_jobs: int
    # Slice 3.1 (T7): the account's tracked applications (cards). Defaulted so the field lands before
    # anything fills it; `AccountDataPort.count_account`'s adapter fills it from T17. A count, never a
    # tracking type.
    tracked_applications: int = 0


# --------------------------------------------------------------------------------------------------
# Slice 2.3 — deleting one history entry (technical plan §0.6).
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeletedHistoryEntry:
    """What `HistoryEntryDataPort.delete_history_entry` removed, committed, in one transaction: the
    run, its export jobs and — if no other run references it — its posting.

    `export_files` is what the use case must unlink next, and it is the same set the `DELETE … RETURNING`
    removed: each key is **derived** from a deleted job's `(id, format)`, never read from its
    `file_key` column, so a `rendering` or `failed` job's already-written bytes are not missed (the
    purge's rule, ADR-0018 decision 3). A tuple of `FileRef`s, never paths or names.
    """

    export_files: tuple[FileRef, ...]
    export_jobs: int
    posting_deleted: bool
    # Slice 3.1 (T7): whether the run's tracked application (card) went with it — at most one per run.
    # Defaulted so the field lands before anything fills it; the adapter's `DELETE` fills it from T17.
    # A bool, never a tracking type.
    tracked_application_deleted: bool = False


@dataclass(frozen=True, slots=True)
class HistoryEntryErasureReport:
    """What erasing one history entry did (AC-14): counts, and the exception **type names** of the
    unlinks the store refused.

    `AccountErasureReport`'s rules, reused: nothing that can carry a key, a path, a filename or a
    message; failures returned rather than logged, because the use case does not log and the entry
    point turns each into one line. The rows are committed before any unlink is tried, so a failure
    here is an orphan for the operator's sweep, never a reason to fail the deletion.
    """

    export_jobs: int
    files_unlinked: int
    unlink_failures: tuple[str, ...]
    posting_deleted: bool
    # Slice 3.1 (T7). A deliberate exception to this class's "no defaults" rule: the default lets the
    # field land before anything fills it, so every existing report stays valid and behaviour is
    # unchanged. The adapter fills it from T17, and `EraseHistoryEntry` passes it through from T9.
    # Whether the run's tracked application (card) was deleted with the entry. A bool, never a
    # tracking type.
    tracked_application_deleted: bool = False


# --------------------------------------------------------------------------------------------------
# Slice 2.5 — the identity token sweep (technical plan §0.9).
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdentityTokenSweepReport:
    """What one run of the identity token sweep deleted, per kind: expired pending registrations,
    expired password resets and expired logins (technical plan §0.9). Counts only — no id, no
    address, no hash — and no defaults, `PurgeReport`'s rule: a default on a count is how a second
    construction site reports a confident zero.

    No file counts and no failure tuples, unlike `PurgeReport`: these rows own no files, and every
    deletion is one statement per batch whose failure is the run's failure.
    """

    pending_registrations: int
    password_resets: int
    logins: int

    def __post_init__(self) -> None:
        """Refuse a negative count (`InvariantViolated`), per the plan's "counts ≥ 0"."""
        if min(self.pending_registrations, self.password_resets, self.logins) < 0:
            raise InvariantViolated("an identity token sweep cannot delete a negative count")
