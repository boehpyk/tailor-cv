"""`ReclaimOrphanedFiles` against a **real filesystem** (T18b, RED) — pinning the MAJOR defect
R-42 warns about: a `.part` file cannot be represented as a `FileRef` at all (ADR-0011's grammar has
no `.part` alternative), which is exactly why `ScannedFile` carries a base `ref` *plus* a separate
`is_partial` flag. But `ReclaimOrphanedFiles.__call__` calls the same `await self._files.delete(ref)`
for a partial and a non-partial entry alike, and `LocalFileStore._delete_sync` unlinks
`root / ref.key` — the **final** key, never `<key>.part`. Two consequences follow, and this module
pins both, plus the happy path that must not regress:

1. **A `.part` file is never actually removed.** `delete(ref)` unlinks the *final* key with
   `missing_ok=True`; if nothing lives there yet, that call is a silent no-op and the `.part` sibling
   sits on the volume forever, coming back `reclaimed` on every sweep. R-36 promises it is deleted.
2. **If a live file *does* exist at that final key, the sweep deletes it instead of the `.part`.**
   Partials are excluded from `which_are_referenced` by design (R-36's own point — nothing ever
   references a `.part`), so the cross-check that protects every other file cannot see this coming:
   it never gets asked about the *final* key that `.part`'s delete call is about to hit. This is the
   irreversible mistake ADR-0018 decision 6 exists to prevent, and it is reachable exactly as
   `task_acks_late` redelivery: `put` #1 succeeds (`K` exists, a `ready` row references it), the task
   is redelivered, `put` #2 writes `K.part` and dies before `os.replace` — now both `K` and `K.part`
   exist on the same key.

**Why this belongs in a new module rather than the existing `test_reclaim_orphaned_files.py`.** That
file's doubles are recording fakes — `_RecordingFileStorePort.delete` just appends `ref` to a list.
A fake has no filesystem, so it cannot distinguish "unlinked `K`" from "unlinked `K.part`": both are
the same recorded call, `delete(ref)`, with the same `ref`. Both of today's consequences are
therefore invisible to that file by construction — R-42's rule, applied to this specific shape. This
module drives the *real* `LocalFileStore` (`infrastructure/files/local_file_store.py`) and the real
`LocalOrphanFileScanner` (`infrastructure/files/orphan_scanner.py`) over a `tmp_path` root, and reads
the answer off the filesystem itself (`Path.exists()`), never off `OrphanScanReport`. Only
`ExpiredGuestDataPort` stays a hand-written double — it is the one collaborator the use case's own
unit tests already cover in full, and nothing about *this* defect depends on how it is implemented.

**Ages are set with `os.utime`, not mocked**, so the window-plus-grace floor arithmetic
(`RetentionWindow` + `grace: timedelta`) is exercised for real against a real `st_mtime`, exactly as
`ReclaimOrphanedFiles.__call__` step 1 computes it: ``cutoff = now - (window.hours + grace)``. The
`clock` fixture (`tests/conftest.py`) is the real `FixedClock`, whole-second by contract (ADR-0007).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.value_objects import ExpiringGuestSession, RetentionWindow
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner

# `ReclaimOrphanedFiles.__call__` step 1 computes `cutoff = now - (window.hours + grace)`. With the
# defaults below that floor is 48h; every "old" fixture in this module sits well past it (60h) and
# every "young" one, where used, sits well before it — the margin is deliberate so a test failure
# here is never a boundary rounding question, only the defect under test.
_WINDOW_HOURS = 24
_GRACE = timedelta(hours=24)
_OLD_AGE = timedelta(hours=60)

# --- Test helpers ------------------------------------------------------------------------------


def _a_file_ref() -> FileRef:
    """A syntactically valid `FileRef` with nothing behind it yet — same construction as the sibling
    fake-backed test file's `_a_file_ref()`."""
    generated = uuid4()
    hex_digits = generated.hex
    return FileRef(key=f"{hex_digits[0:2]}/{hex_digits[2:4]}/{generated}.pdf")


def _age(path: Path, at: datetime) -> None:
    """Set both atime and mtime to `at`, so `LocalOrphanFileScanner`'s `st_mtime` read — the only
    clock this sweep judges age by (`_describe`'s docstring is explicit that it is deliberately not
    the UUID's own embedded timestamp) — sees a real, aged file rather than a mocked one."""
    timestamp = at.timestamp()
    os.utime(path, (timestamp, timestamp))


def _write_part_file(root: Path, ref: FileRef, *, at: datetime) -> Path:
    """Write `<ref.key>.part` directly — the shape `LocalFileStore.put` leaves behind when a write
    is interrupted between the `open`/`fsync` and the `os.replace` (ADR-0011 §5) — and age it to
    `at`. Written by hand rather than through `LocalFileStore`, because that adapter has no public
    method that stops short of the atomic rename; a `.part` on the real volume is always the residue
    of an *incomplete* write, which this constructs directly instead of trying to interrupt one."""
    part_path = root / f"{ref.key}.part"
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(b"an interrupted write, never fsynced to its final name")
    _age(part_path, at)
    return part_path


@dataclass
class _ExpiredGuestDataDouble:
    """The one collaborator this module keeps as a double — `ExpiredGuestDataPort`'s cross-check.
    Only `which_are_referenced` is this sweep's business (the sibling application-test module's
    module docstring explains why in full); the other three raise if `__call__` ever reaches for
    them, which would itself be a finding unrelated to this defect."""

    referenced: frozenset[FileRef] = field(default_factory=frozenset)

    async def count_expired(self, as_of: datetime) -> int:
        raise AssertionError("not used by this sweep")

    async def list_expired(self, as_of: datetime, limit: int) -> Sequence[ExpiringGuestSession]:
        raise AssertionError("not used by this sweep")

    async def delete_session(self, session_id: GuestSessionId) -> None:
        raise AssertionError("not used by this sweep")

    async def which_are_referenced(self, keys: Sequence[FileRef]) -> frozenset[FileRef]:
        return frozenset(key for key in keys if key in self.referenced)


def _use_case(
    root: Path,
    data: _ExpiredGuestDataDouble,
    clock: FixedClock,
) -> tuple[ReclaimOrphanedFiles, LocalFileStore]:
    store = LocalFileStore(root)
    scanner = LocalOrphanFileScanner(root)
    use_case = ReclaimOrphanedFiles(
        scanner,
        data,
        store,
        clock,
        RetentionWindow(hours=_WINDOW_HOURS),
        _GRACE,
    )
    return use_case, store


# --- 1. An old `.part` file is never actually removed --------------------------------------------


async def test_an_old_part_file_is_actually_gone_from_the_volume_after_a_real_run(
    tmp_path: Path, clock: FixedClock
) -> None:
    """R-36 promises a `.part` past the floor is reclaimed. `LocalFileStore.delete(ref)` unlinks
    `root / ref.key` — the **final** key — never `<ref.key>.part`, so with nothing at the final key
    that call is a no-op `missing_ok` unlink and the `.part` sibling is left exactly where it was.
    Asserted directly against the filesystem (R-42), not against `OrphanScanReport.reclaimed` — the
    report already claims 1, which is consequence 1 in miniature: a count that says "gone" about a
    file still sitting on the volume.
    """
    ref = _a_file_ref()
    at = clock.now() - _OLD_AGE
    part_path = _write_part_file(tmp_path, ref, at=at)
    data = _ExpiredGuestDataDouble()
    use_case, _store = _use_case(tmp_path, data, clock)

    await use_case()

    assert not part_path.exists()


# --- 2. A live file at the same final key survives (the irreversible case) -----------------------


async def test_a_live_file_at_the_same_final_key_survives_a_part_sibling_reclaim(
    tmp_path: Path, clock: FixedClock
) -> None:
    """The one that matters. `put` #1 succeeded — `K` exists, a `ready` row references it, the
    cross-check reports it referenced. `put` #2 was redelivered and died before `os.replace`, leaving
    `K.part` beside it. Nothing ever references a `.part` (R-36), so the `.part` entry skips the
    cross-check entirely and its `delete(ref)` call targets `K` — the same final key — and unlinks
    the **live, referenced** file instead of the temp file that was actually orphaned.

    Both are aged past the floor so both are in scope; the referenced double reports `ref` (not the
    `.part`, which cannot be represented as a `FileRef` and never enters the cross-check batch) as
    referenced. After a real run, `K` must still exist with its original bytes, and `K.part` must not
    exist. Asserted against the filesystem, not the report — this is R-42's exact warning: a report
    field cannot tell "the right file was reclaimed" from "the wrong one was".
    """
    ref = _a_file_ref()
    at = clock.now() - _OLD_AGE
    data = _ExpiredGuestDataDouble(referenced=frozenset({ref}))
    use_case, store = _use_case(tmp_path, data, clock)

    await store.put(ref, b"a stranger's live, referenced export")
    live_path = tmp_path / ref.key
    _age(live_path, at)
    part_path = _write_part_file(tmp_path, ref, at=at)

    await use_case()

    assert live_path.exists(), "the live, referenced file must survive the .part reclaim"
    assert await store.get(ref) == b"a stranger's live, referenced export"
    assert not part_path.exists()


# --- 3. A genuine orphan at a final key is still removed (happy path must not regress) -----------


async def test_a_genuine_orphan_at_a_final_key_is_still_removed(
    tmp_path: Path, clock: FixedClock
) -> None:
    """The ordinary case R-35/R-36's sibling application test already covers against a fake — pinned
    here too, against the real store, so a fix for the two cases above cannot "solve" them by making
    `delete` stop unlinking final keys altogether. A single non-partial file, old enough, unreferenced:
    a real run must remove it from the real volume."""
    ref = _a_file_ref()
    at = clock.now() - _OLD_AGE
    data = _ExpiredGuestDataDouble()
    use_case, store = _use_case(tmp_path, data, clock)

    await store.put(ref, b"nobody points at this any more")
    _age(tmp_path / ref.key, at)

    report = await use_case()

    assert not (tmp_path / ref.key).exists()
    assert report.reclaimed == 1


# --- 4. A symlink at a FileRef-shaped key survives the sweep, and its target is untouched (T18b,
#        second occurrence — the containment-escape shape, this time reachable from the volume) -----


def _age_symlink(path: Path, at: datetime) -> None:
    """Set mtime on the **link itself**. `LocalOrphanFileScanner._walk` reads `entry.stat(follow_
    symlinks=False)`, so aging the target — which is what a plain `os.utime` would do — would age the
    wrong inode and the link would never clear the cutoff regardless of the defect under test."""
    timestamp = at.timestamp()
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)


async def test_a_symlink_at_a_file_ref_shaped_key_pointing_at_a_strangers_live_file_survives_a_real_sweep(
    tmp_path: Path, clock: FixedClock
) -> None:
    """The full pipeline, end to end, for the shape `_resolve_contained`'s own docstring calls "T18b's
    finding a second time": a link planted at a `FileRef`-shaped key, pointed at a **different**
    session's live, referenced file. The link's own key is in no row, so the cross-check clears it as
    unreferenced — the ordinary orphan sweep would then try to reclaim it, and before this fix
    `LocalFileStore` resolved the full path, so "reclaiming" the link actually destroyed whatever it
    pointed at while the link itself survived.

    Two independent defences are exercised together here (each is also proven alone —
    `test_orphan_scanner.py` for the scanner's `ref=None`, `test_local_file_store.py` for
    `_resolve_contained`'s refusal): `LocalOrphanFileScanner` reports the link as `ref=None`, so
    `ReclaimOrphanedFiles` counts it `unrecognized` and never calls `FileStorePort.delete` on it at
    all — the sweep never even reaches the second defence in this run, which is the point of having
    the first one.

    **The assertion that matters most**, exactly as the module docstring for consequence 2 above
    states it: after a real run, the victim's file must still exist, with its original bytes, and the
    link itself must still be a link — never reclaimed, never resolved through.
    """
    victim_ref = _a_file_ref()
    link_ref = _a_file_ref()
    at = clock.now() - _OLD_AGE
    # The victim is REFERENCED — it belongs to a live session the cross-check would spare anyway —
    # so this test proves the link is stopped by its own unrecognisability, not merely because the
    # (different) key it points at happens to be referenced.
    data = _ExpiredGuestDataDouble(referenced=frozenset({victim_ref}))
    use_case, store = _use_case(tmp_path, data, clock)

    await store.put(victim_ref, b"a stranger's live, referenced file")
    victim_path = tmp_path / victim_ref.key
    _age(victim_path, at)

    link_path = tmp_path / link_ref.key
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(victim_path)
    _age_symlink(link_path, at)

    report = await use_case()

    assert victim_path.exists(), "the symlink's target must survive the sweep"
    assert await store.get(victim_ref) == b"a stranger's live, referenced file"
    assert link_path.is_symlink(), "the link itself must be untouched — never reclaimed"
    assert report.unrecognized == 1
    assert report.referenced == 1
    assert report.reclaimed == 0
