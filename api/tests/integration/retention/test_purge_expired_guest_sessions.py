"""Application tests for `PurgeExpiredGuestSessions` (T9, RED) — the use case's **own** contract.

**Scope, and why it is narrower than the spec's original wording.** The SQL adapter
(`infrastructure/persistence/retention/expired_guest_data.py`) does not exist yet — it is T14 — so
this file can only exercise what the use case itself is responsible for: *ordering and counting*
against two hand-written doubles. Everything whose truth lives in the database — AC-9 (a `rendering`
export job's derived key), AC-10/AC-11 (the predicate sparing an unexpired session across all four
tables), AC-12 (the 2.2 tripwire), AC-16 (no aggregate hydration, no PII column in any `SELECT`), and
AC-13's *"no `MissingGreenlet` on the next iteration"* — is **T9b**, written after T14/T15 against the
real adapter and a real Postgres (task-list.md's note on T9 explains the split; R-42 governs T9b).

**Why two doubles that share one log, not two independent ones.** AC-8 is a claim about the
*interleaving* of two ports — "this session's row is committed before this session's first file is
unlinked" — and two separate call lists can each look correct in isolation while their true
interleaving is wrong (e.g. both sessions' rows deleted before either session's files are touched).
`_SharedLog` is the one list both doubles append into, in call order, so a test can assert the exact
merged sequence.

Every assertion below states what `PurgeExpiredGuestSessions.__call__` **should** do per its own
skeleton docstring's "Flow" section (T10 implements it) and feature-spec.md's AC-6, AC-7, AC-8, AC-13,
AC-14, AC-15, AC-17 and R-3, R-4, R-5, R-13, R-15 — never what the (currently `NotImplementedError`)
code was observed doing. Because the skeleton's `__call__` body is an unconditional `raise
NotImplementedError`, every test below is expected to fail on that line: a real red, not a vacuous
pass and not an `ImportError`.

**A decision this file makes, because the spec leaves it open:** `ExpiredGuestDataPort.delete_session`
carries no documented failure type (unlike `FileStorePort.delete`, which names `FileStoreUnavailable`
explicitly) — R-3's own wording is "a lock timeout, serialization failure, a constraint nobody
predicted", none of which is enumerable in advance. So the R-3 double raises a plain `RuntimeError`
from `delete_session`, and the R-15 double raises the *identical* exception type from `list_expired`
instead — a call site the five-step flow never wraps in any per-item `try`/`except` at all. Using the
same exception class in both places is deliberate: it proves the use case's handling is scoped to
*which call this came from*, not to *what kind of exception it is*, which is exactly what "exactly two
failures are caught, both per-item" (the skeleton's own docstring) means in practice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.retention.purge_expired_guest_sessions import (
    PurgeExpiredGuestSessions,
)
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.value_objects import (
    ExpiringGuestSession,
    PurgeReport,
    RetentionWindow,
)
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.clock import FixedClock

# --- Test helpers ------------------------------------------------------------------------------


def _a_session_id() -> GuestSessionId:
    return GuestSessionId(value=uuid4())


def _a_file_ref() -> FileRef:
    """A syntactically valid `FileRef` with nothing behind it — `FileRef`'s grammar
    (`domain/shared/files.py`) is what `__post_init__` checks, and a fresh UUIDv7-shaped key
    satisfies it exactly the way a real one derived from an id would."""
    generated = uuid4()
    hex_digits = generated.hex
    return FileRef(key=f"{hex_digits[0:2]}/{hex_digits[2:4]}/{generated}.pdf")


def _an_instant(hours_before_epoch: int) -> datetime:
    """A distinct, whole-second, timezone-aware instant. The actual value is irrelevant to every
    test here — `list_expired` on the double below does not filter or sort by it, since ordering
    itself is the port's contract, proved at T9b (see module docstring) — so these only need to be
    distinguishable from one another."""
    return datetime(2026, 9, 1, tzinfo=UTC) - timedelta(hours=hours_before_epoch)


def _expiring_session(
    *, expires_at: datetime, files: tuple[FileRef, ...] = ()
) -> ExpiringGuestSession:
    return ExpiringGuestSession(session_id=_a_session_id(), expires_at=expires_at, files=files)


@dataclass
class _SharedLog:
    """The one ordered log both doubles below append into — see module docstring for why AC-8 needs
    a single merged sequence rather than two independent call lists."""

    entries: list[tuple[str, GuestSessionId | FileRef]] = field(default_factory=list)


class _CountingClock:
    """Wraps `FixedClock`, counting `now()` calls (AC-6). A use case that read `clock.now()` more
    than once per run could, in production, get two different instants back and disagree with
    itself about when "now" was — the listing, the `expires_at` comparison and (at the entry point,
    outside this use case) the heartbeat all have to be the *same* instant."""

    def __init__(self, inner: FixedClock) -> None:
        self._inner = inner
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return self._inner.now()


@dataclass
class _RecordingExpiredGuestDataPort:
    """A recording `ExpiredGuestDataPort` fake.

    `list_expired` **truncates, it does not sort** — the fixture hands candidates in already
    oldest-first, exactly as the real adapter's `ORDER BY expires_at ASC` would, and it is the real
    adapter's ordering promise that is proved at T9b, not re-proved here (see module docstring).

    `delete_session` mutates its own candidate list on success, the way a real `DELETE` does — which
    is what lets the AC-15 idempotency test call the *same* use case twice and see the second
    `list_expired` come back empty with no separate "already gone" branch needed anywhere.

    `fail_on`, given one session id, makes exactly that session's `delete_session` raise
    `RuntimeError` instead of succeeding — targeted by identity rather than by call count, so the
    failure can land on the *middle* of a longer batch (mirroring `_ConflictingSaveRepository` in
    the export/tailoring sibling files).

    `list_error`, given an exception, makes `list_expired` raise it instead of returning anything —
    R-15's vehicle, since the five-step flow's per-item `try`/`except`es both live inside the loop
    that comes *after* this call.
    """

    candidates: list[ExpiringGuestSession]
    log: _SharedLog
    fail_on: GuestSessionId | None = None
    list_error: Exception | None = None
    list_expired_calls: list[tuple[datetime, int]] = field(default_factory=list)

    async def count_expired(self, as_of: datetime) -> int:
        raise NotImplementedError(
            "count_expired is not exercised by T9 — it backs the health probe"
        )

    async def list_expired(self, as_of: datetime, limit: int) -> Sequence[ExpiringGuestSession]:
        if self.list_error is not None:
            raise self.list_error
        self.list_expired_calls.append((as_of, limit))
        return list(self.candidates[:limit])

    async def delete_session(self, session_id: GuestSessionId) -> None:
        if self.fail_on is not None and session_id == self.fail_on:
            self.log.entries.append(("delete_session_failed", session_id))
            raise RuntimeError(
                "a lock timeout, a serialization failure, or an unpredicted constraint"
            )
        self.candidates = [c for c in self.candidates if c.session_id != session_id]
        self.log.entries.append(("delete_session", session_id))

    async def which_are_referenced(self, keys: Sequence[FileRef]) -> frozenset[FileRef]:
        raise NotImplementedError(
            "which_are_referenced is not exercised by T9 — it backs the orphan sweep"
        )


@dataclass
class _RecordingFileStorePort:
    """A recording `FileStorePort` fake. `fail_on`, given a set of keys, makes `delete` raise
    `FileStoreUnavailable` for exactly those refs and succeed for every other — again targeted by
    identity so a failure can land on one key inside a session that has several."""

    log: _SharedLog
    fail_on: frozenset[FileRef] = field(default_factory=frozenset)

    async def put(self, ref: FileRef, data: bytes) -> None:
        raise NotImplementedError("put is not exercised by T9")

    async def get(self, ref: FileRef) -> bytes:
        raise NotImplementedError("get is not exercised by T9")

    async def delete(self, ref: FileRef) -> None:
        if ref in self.fail_on:
            self.log.entries.append(("delete_failed", ref))
            raise FileStoreUnavailable("disk full")
        self.log.entries.append(("delete", ref))


def _use_case(
    data: _RecordingExpiredGuestDataPort,
    files: _RecordingFileStorePort,
    clock: FixedClock | _CountingClock,
    *,
    batch_limit: int = 100,
    dry_run: bool = False,
) -> PurgeExpiredGuestSessions:
    return PurgeExpiredGuestSessions(
        data,
        files,
        clock,
        RetentionWindow(hours=24),
        batch_limit=batch_limit,
        dry_run=dry_run,
    )


# --- 1. AC-6: one clock.now() per run, and the listing uses that same instant -------------------


async def test_clock_now_is_read_exactly_once_and_the_listing_uses_that_instant(
    clock: FixedClock,
) -> None:
    counting_clock = _CountingClock(clock)
    log = _SharedLog()
    data = _RecordingExpiredGuestDataPort(candidates=[], log=log)
    files = _RecordingFileStorePort(log=log)
    use_case = _use_case(data, files, counting_clock)

    await use_case()

    assert counting_clock.calls == 1
    assert data.list_expired_calls == [(clock.now(), 100)]


# --- 2. AC-7: the use case passes the computed as_of and limit, and honours the order it is given -


async def test_batch_limit_is_passed_through_and_the_use_case_honours_the_order_it_was_given(
    clock: FixedClock,
) -> None:
    """AC-7. Ordering — "oldest `expires_at` first" — is `list_expired`'s own contract, proved
    against the real adapter at T9b; what this use case can be held to is that it passes
    `limit=batch_limit` through unchanged, and that it acts on the batch in the order the port
    handed it back, not that it re-sorts anything. Five candidates are handed to the double already
    oldest-first; `list_expired` truncates rather than sorting (see the double's docstring), so "the
    first two acted on, in that order" is a fact about the use case's own loop.
    """
    log = _SharedLog()
    candidates = [
        _expiring_session(expires_at=_an_instant(hours_before_epoch=h)) for h in (5, 4, 3, 2, 1)
    ]
    data = _RecordingExpiredGuestDataPort(candidates=candidates, log=log)
    files = _RecordingFileStorePort(log=log)
    use_case = _use_case(data, files, clock, batch_limit=2)

    await use_case()

    assert data.list_expired_calls == [(clock.now(), 2)]
    acted_on = [entry[1] for entry in log.entries if entry[0] == "delete_session"]
    assert acted_on == [candidates[0].session_id, candidates[1].session_id]


# --- 3. AC-8, the central one: per session, not per batch ----------------------------------------


async def test_each_sessions_delete_returns_before_that_same_sessions_first_file_is_unlinked(
    clock: FixedClock,
) -> None:
    """AC-8. With two sessions of two and one file respectively, the only ordering that satisfies
    "rows first, committed, then files — per session, not per batch" (ADR-0006 §2) is the exact
    merged sequence asserted below. A batch-wide arrangement — both sessions' rows deleted before
    either session's files are touched — would also pass a weaker test that only checked "every
    `delete_session` precedes every `delete`", which is precisely the shape this design rejects.
    """
    log = _SharedLog()
    file_a1, file_a2, file_b1 = _a_file_ref(), _a_file_ref(), _a_file_ref()
    session_a = _expiring_session(expires_at=_an_instant(0), files=(file_a1, file_a2))
    session_b = _expiring_session(expires_at=_an_instant(1), files=(file_b1,))
    data = _RecordingExpiredGuestDataPort(candidates=[session_a, session_b], log=log)
    files = _RecordingFileStorePort(log=log)
    use_case = _use_case(data, files, clock)

    await use_case()

    assert log.entries == [
        ("delete_session", session_a.session_id),
        ("delete", file_a1),
        ("delete", file_a2),
        ("delete_session", session_b.session_id),
        ("delete", file_b1),
    ]


# --- 4. AC-13 / R-3: a refused delete_session is counted, its files are never unlinked, and the --
# --- batch continues to later sessions ------------------------------------------------------------


async def test_a_refused_delete_session_is_counted_its_files_stay_unlinked_and_later_sessions_still_run(
    clock: FixedClock,
) -> None:
    """AC-13 / R-3. Three sessions; the **middle** one's `delete_session` raises. Its file must never
    reach `files.delete` at all — rows-first means a row that is still there must keep naming a file
    that still exists. The **later** session (listed after the failure) is still deleted and its
    file still unlinked, proving one bad row does not corrupt the rest of the batch — the
    `MissingGreenlet` failure mode CLAUDE.md records is exactly what a batch-wide transaction would
    risk here; the SAVEPOINT half of that guarantee is proved against the real adapter at T9b.
    """
    log = _SharedLog()
    file_first, file_middle, file_last = _a_file_ref(), _a_file_ref(), _a_file_ref()
    first = _expiring_session(expires_at=_an_instant(0), files=(file_first,))
    middle = _expiring_session(expires_at=_an_instant(1), files=(file_middle,))
    last = _expiring_session(expires_at=_an_instant(2), files=(file_last,))
    data = _RecordingExpiredGuestDataPort(
        candidates=[first, middle, last], log=log, fail_on=middle.session_id
    )
    files = _RecordingFileStorePort(log=log)
    use_case = _use_case(data, files, clock)

    report = await use_case()

    assert report.examined == 3
    assert report.sessions_deleted == 2
    assert report.sessions_failed == 1
    assert report.files_unlinked == 2
    assert report.files_failed == 0
    assert report.dry_run is False

    assert log.entries == [
        ("delete_session", first.session_id),
        ("delete", file_first),
        ("delete_session_failed", middle.session_id),
        ("delete_session", last.session_id),
        ("delete", file_last),
    ]


# --- 5. AC-14 / R-4: FileStoreUnavailable is counted, the row stays deleted, and the batch ---------
# --- continues to the session's other files and to later sessions ---------------------------------


async def test_file_store_unavailable_on_unlink_is_counted_the_session_still_counts_deleted_and_the_batch_continues(
    clock: FixedClock,
) -> None:
    """AC-14 / R-4. The row is already deleted and committed by the time `files.delete` runs — the
    crash window ADR-0006 §2 deliberately chose — so a `FileStoreUnavailable` on one key does not
    roll the session back: it still counts `sessions_deleted`, and the session's *other* file
    (listed after the failing one) is still attempted, as is the unrelated next session.

    R-5, as a note rather than its own test: `files_unlinked` counts *keys the use case asked the
    store to remove*, not files that existed — `FileStorePort.delete` is `missing_ok` by contract, so
    nobody downstream of the port can honestly compute "did it exist". `bad_file` below is never
    counted in `files_unlinked`, only in `files_failed`; `good_file` and `other_file` still are.
    """
    log = _SharedLog()
    bad_file, good_file, other_file = _a_file_ref(), _a_file_ref(), _a_file_ref()
    session = _expiring_session(expires_at=_an_instant(0), files=(bad_file, good_file))
    other = _expiring_session(expires_at=_an_instant(1), files=(other_file,))
    data = _RecordingExpiredGuestDataPort(candidates=[session, other], log=log)
    files = _RecordingFileStorePort(log=log, fail_on=frozenset({bad_file}))
    use_case = _use_case(data, files, clock)

    report = await use_case()

    assert report.examined == 2
    assert report.sessions_deleted == 2
    assert report.sessions_failed == 0
    assert report.files_unlinked == 2
    assert report.files_failed == 1
    assert report.dry_run is False

    assert log.entries == [
        ("delete_session", session.session_id),
        ("delete_failed", bad_file),
        ("delete", good_file),
        ("delete_session", other.session_id),
        ("delete", other_file),
    ]


# --- 6. AC-15: a second run over the same (now emptied) double reports all zeroes -----------------


async def test_a_second_run_over_the_same_now_emptied_double_reports_examined_zero(
    clock: FixedClock,
) -> None:
    """AC-15. Idempotency, proved the way the real predicate provides it: once a session is deleted
    it is gone from the store the double simulates, so `list_expired` naturally returns nothing on
    the second call — no "already gone" special case is needed in the use case or in this test.
    """
    log = _SharedLog()
    session = _expiring_session(expires_at=_an_instant(0), files=(_a_file_ref(),))
    data = _RecordingExpiredGuestDataPort(candidates=[session], log=log)
    files = _RecordingFileStorePort(log=log)
    use_case = _use_case(data, files, clock)

    first = await use_case()
    assert first.sessions_deleted == 1

    second = await use_case()

    assert second.examined == 0
    assert second.sessions_deleted == 0
    assert second.sessions_failed == 0
    assert second.files_unlinked == 0
    assert second.files_failed == 0
    assert second.dry_run is False


# --- 7. R-13 / AC-17: a dry run calls neither port's mutating method at all ------------------------


async def test_dry_run_calls_neither_delete_session_nor_files_delete_and_reports_the_would_unlink_total(
    clock: FixedClock,
) -> None:
    """R-13 / AC-17. The **absence** of calls is the acceptance criterion, not a nice-to-have — so
    this asserts the spies directly (`log.entries == []`), rather than inferring "nothing happened"
    from the report's zero counts, which a buggy implementation could produce by other means (e.g.
    calling and discarding the result). `examined` is still the candidate count, and `files_unlinked`
    is the total number of keys across every candidate — what the run *would* have unlinked; the
    entry point is what words that as "would unlink" for a human to read.
    """
    log = _SharedLog()
    session_a = _expiring_session(expires_at=_an_instant(0), files=(_a_file_ref(), _a_file_ref()))
    session_b = _expiring_session(expires_at=_an_instant(1), files=(_a_file_ref(),))
    data = _RecordingExpiredGuestDataPort(candidates=[session_a, session_b], log=log)
    files = _RecordingFileStorePort(log=log)
    use_case = _use_case(data, files, clock, dry_run=True)

    report = await use_case()

    assert log.entries == []
    assert report.examined == 2
    assert report.sessions_deleted == 0
    assert report.sessions_failed == 0
    assert report.files_unlinked == 3
    assert report.files_failed == 0
    assert report.dry_run is True
    assert isinstance(report, PurgeReport)


# --- 8. R-15: an unexpected exception is not caught, and escapes __call__ --------------------------


async def test_an_unexpected_exception_while_listing_propagates_out_of_call(
    clock: FixedClock,
) -> None:
    """R-15. `delete_session` (R-3) and `FileStoreUnavailable` on `files.delete` (R-4) are the only
    two tolerated, per-item failures the skeleton's docstring names; everything else must escape
    uncaught, so the Celery task or the CLI fails loudly instead of returning a green report of
    zeroes while the backlog keeps climbing. `list_expired` is the vehicle here precisely because
    the five-step flow's two named `try`/`except`es both live *inside* the per-candidate loop, which
    this call happens before — there is nothing here for a floor to even wrap.
    """
    log = _SharedLog()
    data = _RecordingExpiredGuestDataPort(candidates=[], log=log, list_error=RuntimeError("boom"))
    files = _RecordingFileStorePort(log=log)
    use_case = _use_case(data, files, clock)

    # `NotImplementedError` is itself a `RuntimeError` subclass, so a bare `pytest.raises(RuntimeError)`
    # would pass vacuously against the skeleton's unconditional `raise NotImplementedError` — proving
    # nothing about whether this specific exception propagated. The exact-type check is what makes
    # this a real red rather than an accidental green.
    with pytest.raises(RuntimeError) as exc_info:
        await use_case()
    assert type(exc_info.value) is RuntimeError
