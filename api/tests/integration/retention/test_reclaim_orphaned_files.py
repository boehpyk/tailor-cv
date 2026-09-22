"""Application tests for `ReclaimOrphanedFiles` (T12, RED) — the orphan sweep's own contract.

**Scope.** Like T9's sibling file, this exercises only what the use case itself is responsible for,
against three hand-written doubles: `OrphanFileScannerPort`, `ExpiredGuestDataPort` (only
`which_are_referenced` matters here — see below) and `FileStorePort`. There is no real filesystem
walk and no real Postgres cross-check in this file; those belong to the adapters (T14/T15,
test-after) and to a T12b sibling the same way T9b follows T9.

**The trap this file is written against.** `__call__`'s body is an unconditional `raise
NotImplementedError` (skeleton step). Any assertion that only checks the *absence* of a side effect
— "the dry run never calls `files.delete`", "an aborted scan unlinks nothing" — is trivially true of
a method that does nothing at all, and would still be true if T13 shipped a completely broken
implementation that also did nothing. So **every absence assertion below is paired, in the same
test, with a positive assertion that discriminates**: a report field with the right count, or
`OrphanScanAborted` actually being the type raised. Each test calls the use case, asserts the
positive fact first (which fails now, on `NotImplementedError` propagating past it), and only then
asserts the absence — so a reader can see the discriminating half is not incidental.

**Why `which_are_referenced` is the only `ExpiredGuestDataPort` method this sweep may call.** The
other three (`count_expired`, `list_expired`, `delete_session`) are the *purge's* business, not the
orphan sweep's — `ReclaimOrphanedFiles` never lists or deletes a `GuestSession` at all, it walks the
volume and cross-checks storage keys. Each of the other three raises `AssertionError("not used by
this sweep")` on the double below: if T13 ever calls one, the test fails on that assertion rather
than looking like a working port with more capability than the use case is supposed to use.

**Why `OrphanScanAborted` is safe to `pytest.raises` on without an exact-type pin, and it is pinned
anyway.** `OrphanScanAborted`'s MRO is `OrphanScanAborted -> RetentionError -> DomainError ->
Exception`; `DomainError` inherits directly from `Exception`, not from `RuntimeError`. The
skeleton's `NotImplementedError` is a `RuntimeError` subclass, which shares no ancestor with
`DomainError` below `Exception` itself — so `pytest.raises(OrphanScanAborted)` cannot be satisfied
by the skeleton's `NotImplementedError` the way T9's `test_an_unexpected_exception_while_listing...`
warned a bare `pytest.raises(RuntimeError)` could be. The exact-type assertion is kept anyway,
purely for the same self-documenting reason T9 kept it: a reader should not have to re-derive the
MRO argument above to see that the check is precise.

**Why the scanner double does not filter by `cutoff` itself.** `OrphanFileScannerPort.scan_older_than`
promises to return entries at or before `cutoff` — but proving the *use case* applies the
window-plus-grace floor correctly (R-34, AC-24) requires handing it entries on both sides of that
floor and letting the use case's own step-4 age check decide, exactly as `_RecordingExpiredGuestData
Port.list_expired` in T9 truncates rather than sorting so the use case's own ordering is what gets
proved. The double here therefore returns a fixed list regardless of the `cutoff` it was asked for,
and records every `cutoff` it was called with so the floor arithmetic itself can be pinned.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.errors import OrphanScanAborted
from tailorcraft.domain.retention.value_objects import (
    ExpiringGuestSession,
    OrphanScanReport,
    RetentionWindow,
    ScannedFile,
)
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.clock import FixedClock

# --- Test helpers ------------------------------------------------------------------------------


def _a_file_ref() -> FileRef:
    """A syntactically valid `FileRef` with nothing behind it — see the identical helper in T9's
    `test_purge_expired_guest_sessions.py` for why a fresh UUIDv7-shaped key is enough."""
    generated = uuid4()
    hex_digits = generated.hex
    return FileRef(key=f"{hex_digits[0:2]}/{hex_digits[2:4]}/{generated}.pdf")


def _scanned_file(
    *, ref: FileRef | None, created_at: datetime, is_partial: bool = False
) -> ScannedFile:
    return ScannedFile(ref=ref, created_at=created_at, is_partial=is_partial)


@dataclass
class _RecordingOrphanFileScannerPort:
    """A recording `OrphanFileScannerPort` fake. Returns `entries` unchanged regardless of the
    `cutoff` it is asked for (see module docstring for why), and records every `cutoff` it was
    called with in `scan_calls` — the vehicle for pinning the floor arithmetic (R-34)."""

    entries: list[ScannedFile]
    scan_calls: list[datetime] = field(default_factory=list)
    error: Exception | None = None

    async def scan_older_than(self, cutoff: datetime) -> Sequence[ScannedFile]:
        self.scan_calls.append(cutoff)
        if self.error is not None:
            raise self.error
        return list(self.entries)


@dataclass
class _RecordingExpiredGuestDataPort:
    """A recording `ExpiredGuestDataPort` fake. Only `which_are_referenced` is this sweep's
    business (see module docstring) — the other three methods raise `AssertionError("not used by
    this sweep")`, which is itself a useful assertion if `__call__` ever reaches for one of them.

    `which_are_referenced` returns the subset of `keys` present in `referenced`, and records every
    call's arguments (as a `frozenset`, since nothing in the contract promises an order) in
    `which_are_referenced_calls` — the vehicle for R-36's "never appears in the cross-check's
    argument" assertion.
    """

    referenced: frozenset[FileRef] = field(default_factory=frozenset)
    which_are_referenced_calls: list[frozenset[FileRef]] = field(default_factory=list)
    error: Exception | None = None

    async def count_expired(self, as_of: datetime) -> int:
        raise AssertionError("not used by this sweep")

    async def list_expired(self, as_of: datetime, limit: int) -> Sequence[ExpiringGuestSession]:
        raise AssertionError("not used by this sweep")

    async def delete_session(self, session_id: GuestSessionId) -> None:
        raise AssertionError("not used by this sweep")

    async def which_are_referenced(self, keys: Sequence[FileRef]) -> frozenset[FileRef]:
        asked = frozenset(keys)
        self.which_are_referenced_calls.append(asked)
        if self.error is not None:
            raise self.error
        return frozenset(k for k in keys if k in self.referenced)


@dataclass
class _RecordingFileStorePort:
    """A recording `FileStorePort` fake. `fail_on` makes `delete` **or** `delete_partial` raise
    `FileStoreUnavailable` for exactly those refs (R-38); every other call succeeds and is appended
    to `deleted` or `deleted_partial` respectively, in call order — **two separate lists, one per
    method**, so an assertion can see which method the use case actually called rather than infer it
    from a single merged list (T18c). Before T18c this fake had one `deleted` list fed by `delete`
    alone, which could not tell a correct `delete_partial(part_ref)` apart from the bug this slice
    fixed — a plain `delete(part_ref)` that unlinked the *final* key while leaving the `.part` behind
    and still landed `part_ref` in `deleted`, because the fake could not distinguish the two calls
    either. Two tests below asserted `part_ref in files.deleted` and so passed against both the bug
    and the fix; they are corrected in the same commit that adds this second list.
    """

    deleted: list[FileRef] = field(default_factory=list)
    deleted_partial: list[FileRef] = field(default_factory=list)
    fail_on: frozenset[FileRef] = field(default_factory=frozenset)

    async def put(self, ref: FileRef, data: bytes) -> None:
        raise NotImplementedError("put is not exercised by T12")

    async def get(self, ref: FileRef) -> bytes:
        raise NotImplementedError("get is not exercised by T12")

    async def delete(self, ref: FileRef) -> None:
        if ref in self.fail_on:
            raise FileStoreUnavailable("disk full")
        self.deleted.append(ref)

    async def delete_partial(self, ref: FileRef) -> None:
        if ref in self.fail_on:
            raise FileStoreUnavailable("disk full")
        self.deleted_partial.append(ref)


def _use_case(
    scanner: _RecordingOrphanFileScannerPort,
    data: _RecordingExpiredGuestDataPort,
    files: _RecordingFileStorePort,
    clock: FixedClock,
    *,
    window_hours: int = 24,
    grace_hours: int = 24,
    limit: int | None = None,
    dry_run: bool = False,
) -> ReclaimOrphanedFiles:
    return ReclaimOrphanedFiles(
        scanner,
        data,
        files,
        clock,
        RetentionWindow(hours=window_hours),
        timedelta(hours=grace_hours),
        limit=limit,
        dry_run=dry_run,
    )


# --- 1. AC-23 / R-33: fails closed ---------------------------------------------------------------


async def test_a_cross_check_failure_aborts_the_sweep_and_deletes_nothing(
    clock: FixedClock,
) -> None:
    """AC-23 / R-33. The discriminating positive assertion is `OrphanScanAborted` actually being the
    *type* raised out of `__call__` — see the module docstring for why that cannot be satisfied
    vacuously by the skeleton's `NotImplementedError`. It is paired with the absence assertion
    `files.deleted == []`: on its own that half would pass against a skeleton that does nothing at
    all, which is exactly the trap this file is written against.
    """
    orphan_ref, part_ref = _a_file_ref(), _a_file_ref()
    old = clock.now() - timedelta(hours=60)
    scanner = _RecordingOrphanFileScannerPort(
        entries=[
            _scanned_file(ref=orphan_ref, created_at=old),
            _scanned_file(ref=part_ref, created_at=old, is_partial=True),
        ]
    )
    data = _RecordingExpiredGuestDataPort(error=RuntimeError("db unreachable"))
    files = _RecordingFileStorePort()
    use_case = _use_case(scanner, data, files, clock)

    with pytest.raises(OrphanScanAborted) as exc_info:
        await use_case()

    assert type(exc_info.value) is OrphanScanAborted
    assert files.deleted == []


# --- 2. AC-22: a mixed volume produces the right report ------------------------------------------


async def test_a_mixed_volume_produces_the_right_report_and_reclaims_only_true_orphans(
    clock: FixedClock,
) -> None:
    """AC-22. Five entries, one of each kind the failure contract enumerates: referenced (R-35),
    too young (R-34), unrecognized (R-37), a reclaimable `.part` (R-36) and a genuine orphan. Every
    field of `OrphanScanReport` is asserted, and exactly which keys were unlinked is asserted by
    identity, not by count alone — a `reclaimed == 2` that quietly unlinked the wrong two files would
    still satisfy a bare count assertion.

    **Corrected in T18c.** This used to assert `set(files.deleted) == {part_ref, orphan_ref}` —
    i.e. that the sweep called plain `delete()` for the `.part` entry. That was wrong: it was written
    against a fake that fed both `delete` and a partial's deletion into the same list, so it could
    not tell `delete(part_ref)` apart from `delete_partial(part_ref)` and ended up ratifying the T18b
    bug (plain `delete(ref)` for a partial unlinks the *final* key, not the `.part`, while being
    reported reclaimed). The use case won: a partial goes to `delete_partial`, a final key goes to
    `delete`, and the two are now asserted against separate lists so the distinction is visible here
    rather than inferred.
    """
    old = clock.now() - timedelta(hours=60)
    young = clock.now() - timedelta(hours=1)
    referenced_ref = _a_file_ref()
    too_young_ref = _a_file_ref()
    orphan_ref = _a_file_ref()
    part_ref = _a_file_ref()
    scanner = _RecordingOrphanFileScannerPort(
        entries=[
            _scanned_file(ref=referenced_ref, created_at=old),
            _scanned_file(ref=too_young_ref, created_at=young),
            _scanned_file(ref=None, created_at=old),
            _scanned_file(ref=part_ref, created_at=old, is_partial=True),
            _scanned_file(ref=orphan_ref, created_at=old),
        ]
    )
    data = _RecordingExpiredGuestDataPort(referenced=frozenset({referenced_ref}))
    files = _RecordingFileStorePort()
    use_case = _use_case(scanner, data, files, clock)

    report = await use_case()

    assert report == OrphanScanReport(
        scanned=5,
        referenced=1,
        too_young=1,
        unrecognized=1,
        reclaimed=2,
        failed=0,
        dry_run=False,
    )
    assert files.deleted == [orphan_ref]
    assert files.deleted_partial == [part_ref]


# --- 3. R-36: a .part file is reclaimed without entering the cross-check -------------------------


async def test_a_partial_file_past_the_floor_is_reclaimed_without_entering_the_cross_check(
    clock: FixedClock,
) -> None:
    """R-36. `.part` files are never referenced by any row (ADR-0011 §5), so asking the database
    about one would be a pointless round trip — and the class docstring is explicit that
    `is_partial` is a flag on the element *instead of* a second port method for exactly this reason.
    The positive assertion is the reclaim (`files.deleted_partial == [part_ref]`); the absence
    assertion is that `part_ref` never appears in any `which_are_referenced` call, asserted by
    checking every call this sweep made, not just the first — a bug that cross-checks it on a
    *later* batched call would slip past a check of only `which_are_referenced_calls[0]`.

    **Corrected in T18c.** This used to assert `files.deleted == [part_ref]`, i.e. that the sweep's
    unlink for a `.part` entry was a plain `delete(part_ref)` call — the T18b bug itself, which
    unlinks the *final* key and leaves the `.part` on disk while still reporting it reclaimed. The
    use case won: `FileStorePort.delete_partial` exists precisely so a partial's unlink cannot be
    confused with a final key's, so the positive assertion now names `delete_partial`, and
    `files.deleted == []` is added to make the absence half explicit rather than merely omitted.
    """
    part_ref = _a_file_ref()
    old = clock.now() - timedelta(hours=60)
    scanner = _RecordingOrphanFileScannerPort(
        entries=[_scanned_file(ref=part_ref, created_at=old, is_partial=True)]
    )
    data = _RecordingExpiredGuestDataPort()
    files = _RecordingFileStorePort()
    use_case = _use_case(scanner, data, files, clock)

    report = await use_case()

    assert report.reclaimed == 1
    assert files.deleted_partial == [part_ref]
    assert files.deleted == []
    assert all(part_ref not in call for call in data.which_are_referenced_calls)


# --- 4. R-34 / AC-24: the floor is window.hours + grace, never expiry_cutoff ----------------------


async def test_the_age_floor_is_window_hours_plus_grace_not_expiry_cutoff(
    clock: FixedClock,
) -> None:
    """R-34 / AC-24. With `window.hours=24` and `grace=24h` the floor is 48 h. A file at `now - 47h`
    is younger than the floor (too young); a file at `now - 49h` is older (old enough to be
    considered — and, being unreferenced here, reclaimed). Neither file is in the referenced set, so
    this isolates the age arithmetic from the cross-check entirely.

    This is the test that catches an implementation built on `RetentionWindow.expiry_cutoff` instead
    of `now - (window.hours + grace)`: `expiry_cutoff` returns `now` unchanged by design (its own
    docstring explains why at length — the window was already applied once, at `GuestSession.start`),
    so a caller that reached for it here would compute a floor of `now` itself, which would make
    *both* of these files — and every other file on the volume — look old enough, and the two
    entries below would both come out `reclaimed` instead of one `too_young` and one `reclaimed`.
    `scanner.scan_calls` pins the exact cutoff `__call__` asked the scanner for, independent of what
    the entries' own `created_at` happen to be.
    """
    too_young_ref, old_enough_ref = _a_file_ref(), _a_file_ref()
    at_47h = clock.now() - timedelta(hours=47)
    at_49h = clock.now() - timedelta(hours=49)
    scanner = _RecordingOrphanFileScannerPort(
        entries=[
            _scanned_file(ref=too_young_ref, created_at=at_47h),
            _scanned_file(ref=old_enough_ref, created_at=at_49h),
        ]
    )
    data = _RecordingExpiredGuestDataPort()
    files = _RecordingFileStorePort()
    use_case = _use_case(scanner, data, files, clock, window_hours=24, grace_hours=24)

    report = await use_case()

    expected_cutoff = clock.now() - timedelta(hours=48)
    assert scanner.scan_calls == [expected_cutoff]
    assert report.too_young == 1
    assert report.reclaimed == 1
    assert files.deleted == [old_enough_ref]


# --- 5. R-35: a referenced file is skipped and counted, whatever its age -------------------------


async def test_a_referenced_file_is_skipped_and_counted_whatever_its_age(
    clock: FixedClock,
) -> None:
    """R-35. The cross-check doing its job: a file well past the floor, but present in the
    referenced set, must not be unlinked. The positive assertion is `report.referenced == 1`; the
    absence assertion is that `referenced_ref` never reaches `files.delete` — asserted by identity
    (`files.deleted == []`), not merely by `reclaimed == 0`, which a bug that reclaimed a *different*
    ref instead could also satisfy.
    """
    referenced_ref = _a_file_ref()
    old = clock.now() - timedelta(hours=60)
    scanner = _RecordingOrphanFileScannerPort(
        entries=[_scanned_file(ref=referenced_ref, created_at=old)]
    )
    data = _RecordingExpiredGuestDataPort(referenced=frozenset({referenced_ref}))
    files = _RecordingFileStorePort()
    use_case = _use_case(scanner, data, files, clock)

    report = await use_case()

    assert report.referenced == 1
    assert report.reclaimed == 0
    assert files.deleted == []


# --- 6. R-37 / AC-24: an unrecognized entry is skipped, never deleted, never named -----------------


async def test_an_unrecognized_entry_is_skipped_never_deleted_and_never_appears_as_a_name(
    clock: FixedClock,
) -> None:
    """R-37 / AC-24. `ScannedFile.ref` is `FileRef | None` and the type carries **no name field at
    all** (see its class docstring) — an unrecognised file structurally cannot appear anywhere in
    this test as a string, because there is nothing to construct one from; that is what makes "never
    named" true by construction rather than by this test remembering not to print one. What *is*
    assertable is the count and the fact that it never reaches `files.delete`. A second, recognised
    orphan sits alongside it as the discriminating positive control: if the use case wrongly skipped
    *everything* rather than specifically the unrecognised entry, `report.reclaimed` would be 0
    instead of 1 and `files.deleted` would be empty instead of naming the real orphan.
    """
    orphan_ref = _a_file_ref()
    old = clock.now() - timedelta(hours=60)
    scanner = _RecordingOrphanFileScannerPort(
        entries=[
            _scanned_file(ref=None, created_at=old),
            _scanned_file(ref=orphan_ref, created_at=old),
        ]
    )
    data = _RecordingExpiredGuestDataPort()
    files = _RecordingFileStorePort()
    use_case = _use_case(scanner, data, files, clock)

    report = await use_case()

    assert report.unrecognized == 1
    assert report.reclaimed == 1
    assert files.deleted == [orphan_ref]


# --- 7. R-38: a per-file unlink failure is counted and the sweep continues -----------------------


async def test_a_per_file_unlink_failure_is_counted_failed_and_the_sweep_continues(
    clock: FixedClock,
) -> None:
    """R-38. Three genuine orphans; the middle one's `files.delete` raises `FileStoreUnavailable`.
    The positive assertions are `failed == 1` and `reclaimed == 2`; the absence half is that the
    failing ref never appears in `files.deleted` while its neighbours — listed both before and after
    it — do, proving the sweep does not abandon the rest of the volume over one unreadable file
    (mirroring AC-14's per-file continuation in the purge's own sibling test).
    """
    first_ref, bad_ref, last_ref = _a_file_ref(), _a_file_ref(), _a_file_ref()
    old = clock.now() - timedelta(hours=60)
    scanner = _RecordingOrphanFileScannerPort(
        entries=[
            _scanned_file(ref=first_ref, created_at=old),
            _scanned_file(ref=bad_ref, created_at=old),
            _scanned_file(ref=last_ref, created_at=old),
        ]
    )
    data = _RecordingExpiredGuestDataPort()
    files = _RecordingFileStorePort(fail_on=frozenset({bad_ref}))
    use_case = _use_case(scanner, data, files, clock)

    report = await use_case()

    assert report.reclaimed == 2
    assert report.failed == 1
    assert bad_ref not in files.deleted
    assert set(files.deleted) == {first_ref, last_ref}


# --- 8. R-39 / AC-22: a dry run computes every count and deletes nothing --------------------------


async def test_dry_run_computes_every_count_identically_and_deletes_nothing(
    clock: FixedClock,
) -> None:
    """R-39 / AC-22. The same mixed volume as the AC-22 happy-path test, run with `dry_run=True`.
    Every count must come out **identical** to a real run over the same volume — that identity is
    the discriminating positive assertion, and it is what makes the paired absence assertion
    (`files.deleted == []`) meaningful rather than vacuous: a skeleton, or a broken implementation,
    that simply never calls `files.delete` at all would pass the absence half regardless of whether
    the counts were computed correctly. `report.dry_run` is asserted `True` as the field the report
    carries so the caller need not remember which kind of run it read.
    """
    old = clock.now() - timedelta(hours=60)
    young = clock.now() - timedelta(hours=1)
    referenced_ref, too_young_ref, orphan_ref, part_ref = (
        _a_file_ref(),
        _a_file_ref(),
        _a_file_ref(),
        _a_file_ref(),
    )
    scanner = _RecordingOrphanFileScannerPort(
        entries=[
            _scanned_file(ref=referenced_ref, created_at=old),
            _scanned_file(ref=too_young_ref, created_at=young),
            _scanned_file(ref=None, created_at=old),
            _scanned_file(ref=part_ref, created_at=old, is_partial=True),
            _scanned_file(ref=orphan_ref, created_at=old),
        ]
    )
    data = _RecordingExpiredGuestDataPort(referenced=frozenset({referenced_ref}))
    files = _RecordingFileStorePort()
    use_case = _use_case(scanner, data, files, clock, dry_run=True)

    report = await use_case()

    assert report == OrphanScanReport(
        scanned=5,
        referenced=1,
        too_young=1,
        unrecognized=1,
        reclaimed=2,
        failed=0,
        dry_run=True,
    )
    assert files.deleted == []
    assert isinstance(report, OrphanScanReport)


# --- 9. `limit`: at most N orphans are unlinked ---------------------------------------------------


async def test_limit_bounds_how_many_orphans_this_run_unlinks(clock: FixedClock) -> None:
    """`limit`. Three equally-eligible orphans, `limit=2`. The positive assertion is
    `report.reclaimed == 2` (not 3 — proving the bound actually bites); the absence half is that the
    third candidate's ref never reaches `files.delete`, asserted by identity so a bug that unlinked
    the *wrong* two files could not slip past a bare length check.
    """
    first_ref, second_ref, third_ref = _a_file_ref(), _a_file_ref(), _a_file_ref()
    old = clock.now() - timedelta(hours=60)
    scanner = _RecordingOrphanFileScannerPort(
        entries=[
            _scanned_file(ref=first_ref, created_at=old),
            _scanned_file(ref=second_ref, created_at=old),
            _scanned_file(ref=third_ref, created_at=old),
        ]
    )
    data = _RecordingExpiredGuestDataPort()
    files = _RecordingFileStorePort()
    use_case = _use_case(scanner, data, files, clock, limit=2)

    report = await use_case()

    assert report.reclaimed == 2
    assert len(files.deleted) == 2
    assert third_ref not in files.deleted
