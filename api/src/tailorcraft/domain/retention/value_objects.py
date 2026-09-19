"""Value objects for the `retention` bounded context: the window, one expired session, two reports.

**SKELETON step.** The field declarations below are real — a dataclass field *is* its contract, and
`qa`'s RED test (T4) has to be able to construct these types and read their annotations or it fails
on a `TypeError` from the stub rather than on the assertion it was written for. What is deferred is
*logic*: `RetentionWindow.__post_init__` and `RetentionWindow.expiry_cutoff` raise
`NotImplementedError` until the GREEN step fills them in (docs/sdlc.md §2).

The theme running through this module is that **the type is the privacy control**. Everything the
purge and the orphan sweep hand back upward is ids, instants, counts and flags: there is nowhere in
any of these shapes to put a CV, a cover letter, an `original_filename` or a filesystem path, so no
caller downstream — no log line, no health payload, no Sentry frame — can leak one by accident
(AC-4, AC-16, Constitution §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.files import FileRef


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
        raise NotImplementedError

    def expiry_cutoff(self, now: datetime) -> datetime:
        """The instant a session's `expires_at` must be at or before for it to count as expired.

        `now` arrives from the `Clock` port — **never** `datetime.now()`, which is what makes every
        rule in here testable without sleeping or freezing the process (AC-3, `domain/shared/clock.py`).
        The caller passes one instant for a whole run (AC-6), so a run cannot disagree with itself
        about when "now" was.

        The comparison this feeds is **inclusive** at the boundary: a session whose `expires_at` is
        exactly the cutoff is expired and is selected (R-18), matching `GuestSession.is_expired`'s
        own `at >= expires_at` rather than inventing a second, subtly different rule one layer up.
        """
        raise NotImplementedError


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

    `dry_run` is a field rather than something the caller remembers, so a report can never be read as
    a record of deletions that a dry run did not perform (R-13).
    """

    examined: int
    sessions_deleted: int
    sessions_failed: int
    files_unlinked: int
    files_failed: int
    duration_ms: int
    dry_run: bool


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
