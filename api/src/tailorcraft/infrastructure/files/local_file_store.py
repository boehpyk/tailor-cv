"""`LocalFileStore` — the `FileStorePort` adapter backed by a local, shared volume (ADR-0011).

Everything here is synchronous filesystem I/O (`os.open`, `os.fsync`, `os.fchmod`, `os.replace`,
`Path.resolve`, `Path.is_symlink`) wrapped in `asyncio.to_thread`. A blocking write on the event
loop is the exact failure Constitution §1 names as CRITICAL: it looks fine with one concurrent
upload and collapses under five, presenting as "the app is slow" rather than as an error — so every
syscall in this module runs off the loop, not just the obvious `write`.

**All four paths are link-safe, and by the same mechanism rather than by four arguments.** `put` and
`get` open their descriptor `O_NOFOLLOW` and work on the descriptor; `delete` and `delete_partial`
call `unlink`, which removes a symlink itself and never its target. `_resolve_contained` refuses a
link at the final component on top of that — but it is a check-then-use, so it is the *outer* lock
and never the only one. Planting a link needs prior write access to the volume `api` and `worker`
mount, so this is blast-radius reduction rather than a remote exploit; it is uniform so that no
reader has to work out which of the four paths was the safe one.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable, StoredFileMissing

if TYPE_CHECKING:
    from tailorcraft.domain.shared.files import FileStorePort

log = structlog.get_logger(__name__)


def _nofollow_opener(path: str, flags: int) -> int:
    """`open()`'s `opener` hook with `O_NOFOLLOW` added — the module's one link-safe `open`.

    **Why an opener rather than `os.open` plus `open(fd, ...)`.** `open()` takes ownership of the
    descriptor the opener returns, so if anything after the syscall raises — building the buffered
    wrapper, decoding a mode — CPython closes it. Opening the descriptor by hand and wrapping it on
    the next line leaves a window where an exception between the two leaks a file descriptor, and a
    leak in an adapter both the API and the worker call per request is the kind that surfaces weeks
    later as `EMFILE` blamed on whatever ran last. It is also simply shorter, and it keeps the mode
    a literal at each call site, so the handle stays precisely typed.

    `0o600` is ignored unless the caller's mode implies `O_CREAT`, which is why one opener serves
    both the read and the write path.
    """
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


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

        `mkdir -p` the two shard directories, write `<key>.part` (opened `O_NOFOLLOW`), `fsync` it,
        `fchmod 0600` it, then `os.replace` it onto the final key. `os.replace` is atomic on the
        same filesystem (and the `.part` file is always a sibling of the target, so it always is):
        a reader never observes a partial file, and a crash between the write and the replace
        leaves only a `.part` for the 1.6 orphan sweep to collect — never a truncated file at the
        real key. `FileRef` is a pure function of the aggregate id (ADR-0011 §1), so a retried `put`
        targets the same key with the same bytes; the atomic replace makes that retry idempotent
        rather than a corruption risk.
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
        """Read the bytes stored at `ref`'s key.

        Two answers, because the caller has two different things to do about them (X-47, X-48). A
        key that resolves to nothing is `StoredFileMissing` and the download handler turns it into
        **410 `export_file_gone`**: the file will not come back, so retrying *this* request helps
        nobody — exporting again does. Every other filesystem failure — `EIO`, `EACCES`, the volume
        unmounted — is `FileStoreUnavailable` and becomes **503**: try again, it may well work.

        The narrower `except` must come first and that ordering is load-bearing rather than
        stylistic: `StoredFileMissing` is a **subclass** of `FileStoreUnavailable` and
        `FileNotFoundError` is an `OSError`, so an `OSError` floor placed above would swallow the
        missing case and quietly answer 503 for ever. The floor stays underneath, where it makes the
        port's promise true by construction (CLAUDE.md: a port that translates *every* failure needs
        a catch-all, not an allow-list); the specific translation sits on top carrying the better
        reason. A caller that only cares "the store failed" still catches `FileStoreUnavailable` and
        needs no edit.
        """
        try:
            return await asyncio.to_thread(self._get_sync, ref)
        except FileNotFoundError as exc:
            # Same fields as every other line here, and the same omission: never the key, never the
            # joined path. A resolved path carries a UUID that identifies one person's document and
            # names the volume layout of the box it ran on (Constitution §8, ADR-0011 §6).
            log.error("file_store.get_missing", errno=exc.errno, root=str(self._root))
            raise StoredFileMissing("stored file is missing") from exc
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

    async def delete_partial(self, ref: FileRef) -> None:
        """Remove `<key>.part` — the incomplete write beside `ref`, never `ref` itself.

        The sibling of `put`'s temporary file, built the same way (`final_path.with_name(name +
        ".part")`) so the two cannot drift: if `put`'s naming ever changes, this breaks in the same
        edit rather than silently sweeping the wrong path for ever.

        It exists because `FileRef` cannot express a `.part` name (the grammar ends
        `\\.(pdf|docx|txt)$`), so the orphan sweep carries a partial as *the base ref plus a flag* —
        and before slice 1.6's T18b it called plain `delete` for both, which unlinked the **final**
        key. The `.part` survived and was reported reclaimed; worse, where a live file sat at that
        key it was deleted, and partials are excluded from the reference cross-check by design, so
        nothing could catch it.

        Missing is not an error, as for `delete`: another writer's `os.replace` consuming the
        `.part` first leaves the world in the state the caller wanted.
        """
        try:
            await asyncio.to_thread(self._delete_partial_sync, ref)
        except OSError as exc:
            log.error("file_store.delete_partial_failed", errno=exc.errno, root=str(self._root))
            raise FileStoreUnavailable("could not delete partial file") from exc

    # -- synchronous helpers, run only via `asyncio.to_thread` above ---------

    def _put_sync(self, ref: FileRef, data: bytes) -> None:
        final_path = self._resolve_contained(ref)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = final_path.with_name(final_path.name + ".part")

        # **`O_NOFOLLOW`, because `_resolve_contained` guards the final key and this is a different
        # name.** A link planted at `<key>.part` would be followed by a plain `open(..., "wb")`:
        # the bytes would land on the link's target, and then `os.replace` — which renames the link
        # rather than following it — would install the *link* at the real key. One upload
        # overwriting a file elsewhere on the volume, and a key that is a link from then on.
        # `O_NOFOLLOW` turns that into `ELOOP`, which `put` already translates to
        # `FileStoreUnavailable`, so nothing above this method needs to know it exists.
        #
        # `os.fsync` and the mode are applied to the **descriptor**, not to the path. `os.chmod` on
        # a path is a second lookup that could resolve to something else than the one just opened;
        # `fchmod` cannot. It also makes the mode independent of the process umask, which
        # `O_CREAT`'s mode argument is not.
        with open(part_path, "wb", opener=_nofollow_opener) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
        os.replace(part_path, final_path)

    def _get_sync(self, ref: FileRef) -> bytes:
        # **`O_NOFOLLOW` here too, and for a sharper reason than on the write path.**
        # `_resolve_contained` has already refused a link at this key — but that is a *check-then-
        # use*, and `read_bytes()` would perform its own second lookup afterwards. Between the two
        # lookups the name can become a link, which is precisely the window `O_NOFOLLOW` exists to
        # close: the check and the use become one syscall, and there is no interval for the name to
        # change meaning in.
        #
        # It also removes an asymmetry that was worse than the race. With only `put` hardened, a
        # reader had to work out for themselves that `delete` and `delete_partial` are safe because
        # `unlink` removes a link rather than its target, and that `get` was the one path still
        # following one — while `get` is the path **every guest's export download takes**. All four
        # are now link-safe by the same mechanism (see the module docstring).
        #
        # Honest threat model, unchanged: planting a link needs prior write access to the uploads
        # volume, so this is blast-radius reduction rather than a remote exploit. A missing key
        # still raises `FileNotFoundError`, which `get` translates to `StoredFileMissing` and the
        # download handler to 410; `ELOOP` is an `OSError` and becomes 503, which is the right
        # answer for "the volume is in a state we refuse to read through".
        with open(self._resolve_contained(ref), "rb", opener=_nofollow_opener) as handle:
            return handle.read()

    def _delete_sync(self, ref: FileRef) -> None:
        self._resolve_contained(ref).unlink(missing_ok=True)

    def _delete_partial_sync(self, ref: FileRef) -> None:
        # `unlink` removes a symlink itself rather than its target, so the `.part` half is safe
        # either way — but `_resolve_contained` still refuses if a link sits at the **final** key,
        # which means a legitimate `.part` beside a planted link is reported `failed` instead of
        # reclaimed. That is the conservative direction on purpose: the sweep's one job is to not
        # delete things it cannot explain (AC-24), and a shard holding a link is a shard nobody has
        # explained yet.
        #
        # The suffix construction is the same expression `_put_sync` uses for its temporary file.
        # Written as one expression in both places on purpose: a `.part` suffix assembled two
        # different ways is a sweep that silently stops finding anything the day one of them
        # changes.
        final_path = self._resolve_contained(ref)
        final_path.with_name(final_path.name + ".part").unlink(missing_ok=True)

    def _resolve_contained(self, ref: FileRef) -> Path:
        """The path `ref` names, refusing a symlink at the final component and refusing anything
        outside the store root.

        **It resolves the *parent* and keeps the basename un-resolved, and that asymmetry is the
        whole point of this function.** It used to resolve the full path, and `Path.resolve()`
        follows symlinks — so what came back was the link's **target**, and every caller then acted
        on the target: `delete` unlinked it and left the link standing, `delete_partial` built a
        `.part` beside it in whatever shard the target happened to live in, `get` read it and `put`
        wrote through it.

        That is T18b's finding a second time — *the thing named was not the thing deleted, and a
        live file beside it died instead* — and slice 1.6 is what made it reachable. Before the
        orphan sweep, `delete` was only ever called with a ref read out of a database row; the sweep
        is the first caller in this codebase that deletes **by a name it discovered on disk**, which
        is what turns a resolve into a deletion primitive. A link planted at a `FileRef`-shaped key
        and pointed at another session's live file has a key that is in no row, so it clears the
        cross-check as unreferenced, and reclaiming it would destroy the target while the orphan
        survived.

        `LocalOrphanFileScanner` now refuses to give a link a `ref` at all, so the sweep cannot
        reach here with one. This check is the second lock: it protects `get`, `put`, `delete` and
        every future caller, and it closes the window between the scan and the unlink, where the
        scanner's answer is already a second old.

        **Honest threat model.** Planting a link requires prior write access to the uploads volume —
        which `api` and `worker` both mount and nothing else does. So this is blast-radius reduction
        and robustness, not a remote exploit. It earns its place because the damage is silent,
        irreversible, and lands on a *different* user's data than the one whose request is running.

        Resolving the parent is still load-bearing: the shard directories are ones this adapter
        creates itself, so resolving them catches a symlinked shard pointing out of the root — the
        containment check below would otherwise be comparing a path that had never been resolved at
        all.
        """
        root_resolved = self._root.resolve()
        requested = self._root / ref.key
        # `strict=False` (the default) — on `put` the shard directories may not exist yet, and a
        # non-existent parent is not an error here, it is Tuesday.
        parent_resolved = requested.parent.resolve()
        if not parent_resolved.is_relative_to(root_resolved):
            # Still `RuntimeError`, and still "a bug, not user input", because `FileRef`'s grammar
            # rejects `..`, a leading `/`, a backslash and any NUL — a traversing key cannot be
            # *constructed* (ADR-0011 §6). The one other way to land here is a symlinked shard
            # directory pointing out of the root, and that is not survivable per file in any useful
            # sense: the store's own layout is then not the one this adapter built, and every key
            # under that shard is equally wrong. A loud failure is the right answer; the sweep does
            # not reach it, because the walk never descends a link and reports it `ref=None`.
            raise RuntimeError(
                "resolved file path escaped the store root — this indicates a bug, "
                "not user input, because FileRef's grammar should have made it impossible"
            )
        candidate = parent_resolved / Path(ref.key).name
        if candidate.is_symlink():
            # **`FileStoreUnavailable`, not `RuntimeError`, and the choice is about who survives.**
            # A planted link is a state of the *volume* — a file somebody else wrote — not a bug in
            # this code and not user input, so `RuntimeError`'s documented meaning does not cover
            # it. `FileStoreUnavailable` is what `FileStorePort` already promises for "the store
            # would not do this": the guest purge counts it `files_failed` and carries on (R-4),
            # the HTTP boundary answers 503, and the orphan sweep's step-4 catch counts it `failed`
            # and carries on. One planted link therefore costs one file, never the whole sweep —
            # which matters because the sweep and the purge are the privacy promise, and R-7
            # already settled the direction: a job that stops running is the expensive failure.
            #
            # **That last behaviour is deliberately left uncited.** A refused unlink is exactly
            # what `failed` means — "we tried to reclaim this and could not" — but the row one
            # reaches for, R-38, is about a *subtree the walk could not read*, and it was amended
            # at T18 to explicitly **not** be counted `failed`, so that one number does not come to
            # mean two things. Citing R-38 for this would quietly undo that amendment for the next
            # reader, and in this codebase the comments are the design record.
            #
            # Never the path and never the key in the message or the log (Constitution §8, R-37):
            # a name on that volume can be a person's name, and this one was chosen by whoever
            # planted it.
            log.error("file_store.symlink_refused", root=str(self._root))
            raise FileStoreUnavailable("refusing to operate through a symlink in the file store")
        return candidate


if TYPE_CHECKING:
    # Proves `LocalFileStore` structurally satisfies `FileStorePort` without an instance —
    # never executed, costs nothing at runtime. See the identical pattern and rationale in
    # `infrastructure/persistence/repositories/identity/guest_session.py`.
    def _assert_implements_file_store_port(store: LocalFileStore) -> None:
        _: FileStorePort = store
