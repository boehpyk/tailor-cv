"""`LocalFileStore` — the `FileStorePort` adapter backed by a local, shared volume (ADR-0011).

Everything here is synchronous filesystem I/O (`open`, `os.fsync`, `os.chmod`, `os.replace`,
`Path.resolve`) wrapped in `asyncio.to_thread`. A blocking write on the event loop is the exact
failure Constitution §1 names as CRITICAL: it looks fine with one concurrent upload and collapses
under five, presenting as "the app is slow" rather than as an error — so every syscall in this module
runs off the loop, not just the obvious `write`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable

if TYPE_CHECKING:
    from tailorcraft.domain.shared.files import FileStorePort

log = structlog.get_logger(__name__)


class LocalFileStore:
    """Stores bytes on a local (named-volume) directory tree, keyed by `FileRef` (ADR-0011).

    `root` is `settings.upload_dir` — the one directory `api` and `worker` both mount. Nothing here
    ever sees the original filename or the CV's content: only a `FileRef` key and a byte string, and
    neither is ever logged (Constitution §8).
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, ref: FileRef, data: bytes) -> None:
        """Write `data` under `ref`'s key, atomically.

        `mkdir -p` the two shard directories, write `<key>.part`, `fsync` it, `chmod 0600` it, then
        `os.replace` it onto the final key. `os.replace` is atomic on the same filesystem (and the
        `.part` file is always a sibling of the target, so it always is): a reader never observes a
        partial file, and a crash between the write and the replace leaves only a `.part` for the
        1.6 orphan sweep to collect — never a truncated file at the real key. `FileRef` is a pure
        function of the aggregate id (ADR-0011 §1), so a retried `put` targets the same key with the
        same bytes; the atomic replace makes that retry idempotent rather than a corruption risk.
        """
        try:
            await asyncio.to_thread(self._put_sync, ref, data)
        except OSError as exc:
            # Never the path, the filename or the bytes — `errno` and the store root are the whole
            # story an operator needs (disk full, permissions, read-only remount), and nothing here
            # is PII (Constitution §8).
            log.error("file_store.put_failed", errno=exc.errno, root=str(self._root))
            raise FileStoreUnavailable("could not store file") from exc

    async def get(self, ref: FileRef) -> bytes:
        """Read the bytes stored at `ref`'s key."""
        try:
            return await asyncio.to_thread(self._get_sync, ref)
        except OSError as exc:
            log.error("file_store.get_failed", errno=exc.errno, root=str(self._root))
            raise FileStoreUnavailable("could not read file") from exc

    async def delete(self, ref: FileRef) -> None:
        """Remove the file at `ref`'s key. Missing is not an error: deleting an already-gone file
        leaves the world in the state the caller wanted, which is what makes a retried delete (or a
        delete that races the 1.6 sweep) idempotent too."""
        try:
            await asyncio.to_thread(self._delete_sync, ref)
        except OSError as exc:
            log.error("file_store.delete_failed", errno=exc.errno, root=str(self._root))
            raise FileStoreUnavailable("could not delete file") from exc

    # -- synchronous helpers, run only via `asyncio.to_thread` above ---------

    def _put_sync(self, ref: FileRef, data: bytes) -> None:
        final_path = self._resolve_contained(ref)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = final_path.with_name(final_path.name + ".part")

        with open(part_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(part_path, 0o600)
        os.replace(part_path, final_path)

    def _get_sync(self, ref: FileRef) -> bytes:
        return self._resolve_contained(ref).read_bytes()

    def _delete_sync(self, ref: FileRef) -> None:
        self._resolve_contained(ref).unlink(missing_ok=True)

    def _resolve_contained(self, ref: FileRef) -> Path:
        """Belt-and-braces path containment: `FileRef`'s own grammar (`domain/shared/files.py`)
        already rejects `..`, a leading `/`, a backslash and any NUL, so a traversing key cannot be
        *constructed* in the first place — this check can only ever fire on a bug, not on user
        input. It stays anyway, as the second lock on a door the type already keeps shut (ADR-0011
        §6): a resolved path that lands outside `root` is refused rather than trusted, one line of
        defence that costs nothing on the path every legitimate call takes.
        """
        root_resolved = self._root.resolve()
        candidate = (self._root / ref.key).resolve()
        if not candidate.is_relative_to(root_resolved):
            raise RuntimeError(
                "resolved file path escaped the store root — this indicates a bug, "
                "not user input, because FileRef's grammar should have made it impossible"
            )
        return candidate


if TYPE_CHECKING:
    # Proves `LocalFileStore` structurally satisfies `FileStorePort` without an instance —
    # never executed, costs nothing at runtime. See the identical pattern and rationale in
    # `infrastructure/persistence/repositories/identity/guest_session.py`.
    def _assert_implements_file_store_port(store: LocalFileStore) -> None:
        _: FileStorePort = store
