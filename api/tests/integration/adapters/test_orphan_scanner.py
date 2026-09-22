"""`LocalOrphanFileScanner` (`OrphanFileScannerPort`), against a **real filesystem** — the layer that
did not exist as its own test module before this file, and the gap `/verify`'s T18b lesson says to
close: a recording fake cannot model the difference between a link and the file it points at, so the
symlink defence added to this adapter and to `LocalFileStore` (`_resolve_contained`'s own comment
calls it "T18b's finding a second time") was covered only through `test_reclaim_orphaned_files_on_
disk.py`'s use-case-level tests, never directly against the scanner whose `ref=None` answer is the
first of the two layers that make a planted link unreclaimable.

**What this file adds that `test_reclaim_orphaned_files_on_disk.py` cannot.** That module drives
`ReclaimOrphanedFiles.__call__` end to end and asserts on the filesystem after a full sweep — the
right level for "does the whole pipeline survive a planted link", and
`test_a_symlink_at_a_file_ref_shaped_key_pointing_at_a_strangers_live_file_survives_a_real_sweep`
(added there alongside this file) is that proof. This module isolates the **scanner's own answer**:
given a symlink on disk, does `scan_older_than` hand back `ScannedFile(ref=None, ...)`? That is worth
proving in isolation because it is the first of two independent defences (`LocalFileStore.
_resolve_contained`'s own refusal, proved in `test_local_file_store.py`, is the second) — either one
alone is enough to stop the T18b-shaped bug, and testing them separately is what proves that, rather
than only proving the pair together happens to work.

Every syscall this adapter makes goes through `asyncio.to_thread` in production; nothing here needs to
prove that again (AC-42 is `test_...` — see the task list for where the loop-liveness proof lives).
This file is about the *answer*, not the *thread*.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner

# A fixed cutoff and a fixed "well past it" age, exactly as `test_reclaim_orphaned_files_on_disk.py`
# uses — the margin is deliberate so a test failure here is never a boundary rounding question.
_CUTOFF = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
_OLD_AGE = timedelta(hours=60)


def _a_file_ref() -> FileRef:
    """A syntactically valid `FileRef` with nothing behind it yet — the same construction every
    sibling retention test file uses."""
    generated = uuid4()
    hex_digits = generated.hex
    return FileRef(key=f"{hex_digits[0:2]}/{hex_digits[2:4]}/{generated}.pdf")


def _age(path: Path, at: datetime) -> None:
    """Set mtime on an ordinary file. `_describe` reads `st_mtime` off the `lstat` the walk already
    holds, so setting it on the file (following any link, which there is none of here) is correct."""
    timestamp = at.timestamp()
    os.utime(path, (timestamp, timestamp))


def _age_symlink(path: Path, at: datetime) -> None:
    """Set mtime on the **link itself**, not on whatever it points at — `LocalOrphanFileScanner._walk`
    reads `entry.stat(follow_symlinks=False)`, so aging the target would age the wrong inode and the
    entry would never clear the cutoff regardless of the defect under test."""
    timestamp = at.timestamp()
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)


def _write_victim(root: Path, *, name: str = "elsewhere.bin") -> Path:
    """A file for a symlink to point at. Its own identity is irrelevant to these tests — the claim
    under test is that the scanner never turns the *link's* name into a `ref` — so it lives outside
    any shard directory the scanner would otherwise walk into on its own."""
    victim = root / name
    victim.write_bytes(b"whatever this points at is irrelevant to the scanner's own answer")
    return victim


# --- A symlink at a FileRef-shaped key: the scanner must refuse it a ref, not merely count it -------


async def test_scan_reports_a_symlink_at_a_file_ref_shaped_key_as_unrecognized(
    tmp_path: Path,
) -> None:
    """AC-24 / the T18b-shape fix, at the scanner. A symlink whose *name* is otherwise a
    syntactically valid `FileRef` key must still come back `ref=None`: `_describe` tests
    `is_symlink` **before** it ever calls `FileRef(...)`, so the fact that the name would parse is
    never reached. This is what lets `ReclaimOrphanedFiles` count it `unrecognized` and never hand it
    to `FileStorePort.delete` at all — belt, not merely braces; `LocalFileStore._resolve_contained`'s
    own refusal (`test_local_file_store.py`) is the braces, for the case this belt ever failed.
    """
    ref = _a_file_ref()
    link_path = tmp_path / ref.key
    link_path.parent.mkdir(parents=True)
    victim = _write_victim(tmp_path)
    link_path.symlink_to(victim)
    _age_symlink(link_path, _CUTOFF - _OLD_AGE)

    scanner = LocalOrphanFileScanner(tmp_path)
    found = await scanner.scan_older_than(_CUTOFF)

    assert len(found) == 1
    assert found[0].ref is None, "a symlink must never be handed back as a recognised FileRef"
    assert found[0].is_partial is False


async def test_scan_reports_a_symlink_named_dot_part_as_unrecognized_not_partial(
    tmp_path: Path,
) -> None:
    """A symlink named `<key>.part` must **also** come back `ref=None`, not `is_partial=True` with a
    real ref. `_describe` computes `is_partial` from the name alone but decides `ref` from
    `is_symlink` first, and `ReclaimOrphanedFiles.__call__`'s own step-4 loop tests `entry.ref is
    None` **before** it ever reads `is_partial` (that file's own comment: "the floor first, and
    before the `ref is None` test... R-34... [then] R-37"). So a symlinked `.part` can never reach
    `delete_partial` by taking the partial branch — it is filtered out as unrecognised first, every
    time, regardless of the order those two checks might be read in.
    """
    ref = _a_file_ref()
    part_path = tmp_path / f"{ref.key}.part"
    part_path.parent.mkdir(parents=True)
    victim = _write_victim(tmp_path)
    part_path.symlink_to(victim)
    _age_symlink(part_path, _CUTOFF - _OLD_AGE)

    scanner = LocalOrphanFileScanner(tmp_path)
    found = await scanner.scan_older_than(_CUTOFF)

    assert len(found) == 1
    assert found[0].ref is None


# --- Control: an ordinary .part file is unaffected — the symlink tests above are isolating the link -


async def test_scan_reports_a_genuine_dot_part_file_with_a_real_ref_and_is_partial_true(
    tmp_path: Path,
) -> None:
    """Without this control, the two symlink tests above could pass for the wrong reason (e.g. a bug
    that made every `.part` name unrecognised, symlink or not). An ordinary, non-symlink `.part` file
    must still come back with a real `ref` and `is_partial=True` — `_describe`'s own docstring: "only
    *unrecognised* entries are `ref=None`"."""
    ref = _a_file_ref()
    part_path = tmp_path / f"{ref.key}.part"
    part_path.parent.mkdir(parents=True)
    part_path.write_bytes(b"an interrupted write, never fsynced to its final name")
    _age(part_path, _CUTOFF - _OLD_AGE)

    scanner = LocalOrphanFileScanner(tmp_path)
    found = await scanner.scan_older_than(_CUTOFF)

    assert len(found) == 1
    assert found[0].ref == ref
    assert found[0].is_partial is True
