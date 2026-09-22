"""`LocalOrphanFileScanner` — the `OrphanFileScannerPort` adapter over the same volume
`LocalFileStore` writes to (ADR-0011 §§4, 5).

It sits beside `LocalFileStore` rather than in `infrastructure/retention/` because it shares that
adapter's root *and* its layout knowledge: two hex shard levels, a UUIDv7 filename, a `.part`
sibling while a write is in flight. Those three facts are one design, and splitting the reader from
the writer is how the reader learns a layout the writer has since changed.

**Every syscall goes through `asyncio.to_thread`** (AC-42, ADR-0011 §5, Constitution §1). `os.scandir`
and `DirEntry.stat` are blocking, and this walk covers a whole volume rather than one file: on the
event loop it would stall *every* concurrent user for the length of the walk, with no error and
nothing logged — the failure this codebase treats as CRITICAL because it presents as "the app is
slow". The synchronous half is a generator that yields chunks and is resumed inside a worker thread,
one chunk per hop, so no single thread hop holds the pool for the length of the volume.

**Nothing here can put a filename anywhere.** `ScannedFile` has no name field (R-37) and this module
never formats `entry.name`, `entry.path` or a resolved path into a log line, an exception or a return
value. The one thing on the uploads volume that might be a person's name is a filename nobody in this
system wrote, so an unrecognised entry leaves as `ref=None` and is counted, never named. `errno` and
exception *types* are the whole of what this module logs (Constitution §8).
"""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from tailorcraft.domain.intake.errors import InvalidFileRef
from tailorcraft.domain.retention.value_objects import ScannedFile
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable

if TYPE_CHECKING:
    from tailorcraft.domain.retention.ports import OrphanFileScannerPort

log = structlog.get_logger(__name__)

# `LocalFileStore.put` writes `<key>.part`, fsyncs it and `os.replace`s it onto the key. A `.part`
# that survived is a write that never completed (ADR-0011 §5). The suffix is written here a second
# time rather than imported, because these two modules must agree and the day they do not is the day
# the sweep stops recognising a half-written file — which is why `test_orphan_scanner` pins it
# against `LocalFileStore`'s own behaviour instead of against this constant.
_PART_SUFFIX: Final = ".part"

# How deep the layout goes: `<root>/<2 hex>/<2 hex>/<uuid>.<ext>`.
_SHARD_DEPTH: Final = 2

# Entries per thread hop. Not a memory bound (see `scan_older_than`) — a *responsiveness* bound: the
# generator is resumed in a worker thread and yields after this many entries, so the awaiting
# coroutine gets a suspension point roughly every 512 `stat` calls instead of one at the end of the
# volume.
_DEFAULT_CHUNK_SIZE: Final = 512


@dataclass(slots=True)
class _ScanFailures:
    """What the walk could not read, in counts and `errno`s — never in names.

    Mutable and passed into the generator because a generator's *return* value is not available to a
    caller that stops at exhaustion, and the alternative (yielding a union of chunk-or-failure) makes
    every consumer branch on a type to count a number.
    """

    directories: int = 0
    entries: int = 0
    errnos: set[int] = field(default_factory=set)

    def record(self, exc: OSError, *, directory: bool) -> None:
        if directory:
            self.directories += 1
        else:
            self.entries += 1
        if exc.errno is not None:
            self.errnos.add(exc.errno)

    @property
    def recorded(self) -> bool:
        return bool(self.directories or self.entries)


class LocalOrphanFileScanner:
    """Walks the local file store and reports what is at or older than a cutoff.

    `root` is `settings.upload_dir` — the same directory `LocalFileStore` is given, and the same one
    `api` and `worker` both mount.
    """

    def __init__(self, root: Path, *, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> None:
        self._root = root
        self._chunk_size = chunk_size

    async def scan_older_than(self, cutoff: datetime) -> Sequence[ScannedFile]:
        """Every entry on the volume whose mtime is at or before `cutoff`, as `ScannedFile` values.

        **Chunked yields, and a `Sequence` return — the two are reconciled here rather than left to
        look like a contradiction.** The port returns a `Sequence`, and
        `ReclaimOrphanedFiles` does `list(scanned)[: limit]` on it, so the whole answer is
        materialized either way: chunking does **not** bound peak memory, and a docstring claiming it
        did would be the kind of comfortable falsehood this codebase's `/verify` keeps finding. What
        it bounds is how long one thread hop runs and how long the event loop waits between
        suspension points. The honest memory bound is `len(volume)` times a three-field frozen
        dataclass, with no name in it — tens of MB for a volume of a million files. If that ever
        stops being acceptable the fix is the port becoming an `AsyncIterator`, which is a domain
        change to argue rather than something this adapter can pretend to have done.

        **The age filter lives here as well as in the use case, and that duplication is deliberate.**
        This method's contract is "at or before the cutoff" (inclusive, like every other boundary in
        this slice), so it filters; `ReclaimOrphanedFiles` filters again and counts `too_young`. With
        this adapter that count is therefore structurally zero, and the outer check is the
        load-bearing one — it is what makes the use case correct against *any* scanner, including a
        future object-store one whose listing has coarser granularity.

        Raises:
            FileStoreUnavailable: the store root itself could not be read. **Deliberately fatal**:
                the use case wraps this call in nothing (R-15), so the command fails and the operator
                learns the volume could not be enumerated. The alternative — reporting an empty scan
                — is how an unmounted volume reads as "0 files, nothing to do", which is exactly the
                clean-looking zero that a sweep must never produce. A missing root is included:
                `LocalFileStore.put` creates it on the first write, so on a box that has ever stored
                a file, an absent root means the volume is gone rather than empty.
        """
        failures = _ScanFailures()
        walker = self._walk(cutoff, failures)
        found: list[ScannedFile] = []
        try:
            try:
                while True:
                    # `next` is what resumes the generator, so every `os.scandir`, every iteration
                    # step and every `stat` inside it runs in the worker thread — not merely the
                    # first one (AC-42).
                    chunk = await asyncio.to_thread(_next_chunk, walker)
                    if chunk is None:
                        break
                    found.extend(chunk)
            finally:
                # Closing the generator runs the `finally` inside it, which closes the open directory
                # handle it was suspended in. That is a syscall too, so it goes off the loop like the
                # rest. After exhaustion it is a no-op; it matters when the caller is cancelled or
                # the loop above raised.
                await asyncio.to_thread(walker.close)
        except OSError as exc:
            # Only the root's own `scandir` reaches here — every subtree failure is caught inside the
            # walk and counted. `errno` and the root, exactly as `LocalFileStore` logs; never a path
            # from inside the tree, which carries a UUID that identifies one person's document.
            log.error("retention.orphan_scan_failed", errno=exc.errno, root=str(self._root))
            raise FileStoreUnavailable("could not read the file store") from exc

        if failures.recorded:
            # R-38's channel, and its limits are stated in the task list rather than papered over
            # here: `OrphanFileScannerPort.scan_older_than` returns a bare `Sequence[ScannedFile]`,
            # and `OrphanScanReport.failed` is computed by the use case from *unlink* failures, so
            # there is nowhere in the current port shape to hand an unreadable subtree upward. One
            # log line, in counts and `errno`s, is therefore the whole channel. Widening the port to
            # carry it is a domain change and belongs to whoever owns that decision.
            log.warning(
                "retention.orphan_scan_unreadable",
                directories=failures.directories,
                entries=failures.entries,
                errnos=sorted(failures.errnos),
            )
        return tuple(found)

    # -- the synchronous walk, resumed only inside `asyncio.to_thread` above -------------------

    def _walk(self, cutoff: datetime, failures: _ScanFailures) -> Generator[list[ScannedFile]]:
        """Yield chunks of entries at or older than `cutoff`, walking two shard levels.

        Depth-limited on purpose. `<root>/<2 hex>/<2 hex>/` is the whole layout, so a directory
        *below* the second level is not part of it; descending into one would turn "somebody mounted
        something under the uploads volume" into an unbounded walk performed by a job that deletes
        things. Files found *above* the second level are still reported, with `ref=None`: they cannot
        form a valid key, they are exactly R-37's "someone dropped a file in", and they are counted
        and left alone.
        """
        chunk: list[ScannedFile] = []
        # Paths to visit, each with the shard names that led to it. A stack of paths rather than of
        # open iterators, so at most one directory handle is open while the generator is suspended.
        pending: list[tuple[Path, tuple[str, ...]]] = [(self._root, ())]

        while pending:
            directory, prefix = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            if len(prefix) < _SHARD_DEPTH:
                                pending.append((Path(entry.path), (*prefix, entry.name)))
                            continue
                        try:
                            # `follow_symlinks=False` everywhere in this module: a symlink is judged
                            # by its own mtime and never descended, so a link planted in the volume
                            # cannot walk this job out of the store root or into a cycle. Anything
                            # that is not a directory is reported — a symlink, a fifo, a socket —
                            # because the honest answer to "what is on this volume" includes the
                            # things nobody expected, and an unrecognised one is only ever counted.
                            #
                            # **Not descending a link is only half of it, and the other half is the
                            # seam.** What this walk reports, `ReclaimOrphanedFiles` may hand to
                            # `FileStorePort.delete` — and that adapter resolves before it unlinks.
                            # So the walk's refusal to follow a link says nothing about what the
                            # *deleter* would follow, which is why `_describe` is told whether this
                            # entry is a link rather than left to infer it from a name.
                            stat_result = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            failures.record(exc, directory=False)
                            continue
                        # Read off the `lstat` we already hold, so it costs no syscall and cannot
                        # disagree with the `st_mtime` taken from the same call.
                        scanned = _describe(
                            entry.name,
                            prefix,
                            stat_result.st_mtime,
                            cutoff,
                            is_symlink=stat_module.S_ISLNK(stat_result.st_mode),
                        )
                        if scanned is None:
                            continue
                        chunk.append(scanned)
                        if len(chunk) >= self._chunk_size:
                            yield chunk
                            chunk = []
            except OSError as exc:
                if not prefix:
                    # The root. Not a subtree we can skip — it is the whole question.
                    raise
                # R-38: skip this subtree and keep going. One unreadable shard directory must not
                # abandon the rest of the volume, and a sweep that stops early reports fewer orphans
                # rather than wrong ones.
                failures.record(exc, directory=True)
                continue

        if chunk:
            yield chunk


def _next_chunk(walker: Generator[list[ScannedFile]]) -> list[ScannedFile] | None:
    """`next(walker, None)`, as a named function so the thread hop is typed.

    `None` means the walk is finished — a chunk is never empty, so there is no ambiguity.
    """
    return next(walker, None)


def _describe(
    name: str,
    prefix: tuple[str, ...],
    mtime: float,
    cutoff: datetime,
    *,
    is_symlink: bool,
) -> ScannedFile | None:
    """Turn one directory entry into a `ScannedFile`, or `None` if it is newer than the cutoff.

    **Age comes from `st_mtime`, not from the UUIDv7 in the filename** — ADR-0011 §4 is right that
    the id carries a timestamp, and it is still the wrong clock to read here. A `.part` we failed to
    name has no id; neither has a file somebody dropped in by hand; and `mtime` is what a restore,
    a `cp -p` and a volume migration preserve. One rule must judge every entry the walk found, so it
    is the one every entry has. Truncated to a whole second (the codebase's timestamp convention,
    ADR-0007), which floors — a file is judged at most a second older than it is, against a floor
    measured in days.

    **A `.part` file gets a real `ref`, derived from the name with the suffix stripped, plus
    `is_partial=True`** (decided at T12; R-36 depends on it). Only *unrecognised* entries are
    `ref=None`. The reason is that R-36's claim — a `.part` is reclaimed **without** appearing in the
    cross-check's argument — is only a claim if a ref existed that could have appeared; with
    `ref=None` the `.part` would be skipped as unrecognised and the rule would be untestable and,
    worse, silently unenforced.

    **`FileRef`'s own constructor is the recognisability test.** Its `fullmatch` against ADR-0011's
    grammar *is* the check, so there is no second grammar in this module to drift from it: the two
    shard names and the filename are joined into a candidate key and handed to the type. A name that
    is not a key raises `InvalidFileRef` and becomes `ref=None` — counted by the use case, never
    deleted, and never named anywhere (R-37).

    **A symlink is unrecognised whatever it is named, and that is AC-24 rather than a new rule.**
    This store writes bytes under generated keys and creates a link nowhere, ever — so a link
    sitting at a `FileRef`-shaped key is by definition a file the sweep *cannot explain*, and AC-24
    says it never deletes one of those. Giving it a `ref` would be worse than useless: the key that
    is in no database row is the **link's**, so the cross-check clears it as unreferenced, and
    `LocalFileStore` resolves before it unlinks — so "reclaiming" it would destroy whatever the link
    points at, which can be another session's live, referenced file, while the link itself survives.

    That is T18b's shape a second time — *the thing named was not the thing deleted, and a live
    file beside it died instead* — and it is newly reachable for the same reason: this sweep is the
    first caller in the codebase that deletes by a name it discovered **on disk** rather than by a
    ref read out of a row. Planting the link needs prior write access to the uploads volume, so
    this is blast radius, not a remote exploit; it is worth closing because the damage is silent,
    irreversible and lands on somebody else's data.

    `is_partial` is still reported honestly for a link named `<key>.part`, and it changes nothing:
    the use case tests `ref is None` first, counts the entry `unrecognized` and leaves it alone.
    Nothing about the link is logged or kept — this branch, like the one above it, is the whole of
    what the system will ever know about it (R-37).
    """
    created_at = datetime.fromtimestamp(int(mtime), tz=UTC)
    if created_at > cutoff:
        return None

    is_partial = name.endswith(_PART_SUFFIX)
    base = name[: -len(_PART_SUFFIX)] if is_partial else name

    ref: FileRef | None = None
    if len(prefix) == _SHARD_DEPTH and not is_symlink:
        try:
            ref = FileRef(f"{prefix[0]}/{prefix[1]}/{base}")
        except InvalidFileRef:
            # Recognisability, answered by the type. Nothing about the refused name is logged or
            # kept: this branch is the whole of what the system will ever know about it.
            ref = None
    return ScannedFile(ref=ref, created_at=created_at, is_partial=is_partial)


if TYPE_CHECKING:
    # Proves `LocalOrphanFileScanner` structurally satisfies `OrphanFileScannerPort` without an
    # instance — never executed, costs nothing at runtime. The same pattern and rationale as
    # `LocalFileStore`'s at the foot of `local_file_store.py`.
    def _assert_implements_orphan_scanner_port(scanner: LocalOrphanFileScanner) -> None:
        _: OrphanFileScannerPort = scanner
