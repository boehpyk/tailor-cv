"""Adapter tests for `LocalFileStore` (`FileStorePort`), written **after** (T29 for `put`; I8t for
`get`): atomic replace, file mode, path-containment rejection, the `ENOSPC` → `FileStoreUnavailable`
translation (technical-plan.md's Adapters row; F-14), and `get`'s two-way failure split (X-47, X-48).

No database and no `clear_redis` needed — this adapter's whole world is a local directory, given
fresh per test via `tmp_path`.

**`get`'s discriminating assertion is easy to get wrong, and the tests below are written to catch
it.** `StoredFileMissing` is a *subclass* of `FileStoreUnavailable`, so `pytest.raises(
FileStoreUnavailable)` on an absent key passes even if `LocalFileStore.get`'s two `except` clauses
were swapped — the narrower `except FileNotFoundError` has to sit *above* the `OSError` floor, or the
floor swallows it and every absent key quietly becomes a 503 instead of a 410. The absent-key test
below asserts the narrower type directly (`pytest.raises(StoredFileMissing)`); the unreadable-file
test asserts the negative on the same axis (`type(exc) is FileStoreUnavailable`, not merely
`isinstance`) — together they are the only pair that cannot pass with the clauses in the wrong order.
"""

from __future__ import annotations

import errno
import logging
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable, StoredFileMissing
from tailorcraft.infrastructure.files import local_file_store
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings


def _ref() -> FileRef:
    return FileRef.for_base_cv(BaseCvId(uuid4()), CvContentType.PDF)


async def test_put_twice_with_the_same_ref_replaces_atomically_and_leaves_one_file(
    tmp_path: Path,
) -> None:
    """F-22's idempotent retry, exercised directly: a second `put` at the same key must not leave a
    stray `.part` sibling behind, and the file at the key must hold the newer bytes — proof the
    `os.replace` swap is really atomic rather than merely "usually fine"."""
    store = LocalFileStore(tmp_path)
    ref = _ref()

    await store.put(ref, b"first version")
    await store.put(ref, b"second version")

    final_path = tmp_path / ref.key
    assert await store.get(ref) == b"second version"
    assert list(final_path.parent.iterdir()) == [final_path]


async def test_put_writes_the_file_with_mode_0600(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    ref = _ref()

    await store.put(ref, b"content")

    mode = stat.S_IMODE((tmp_path / ref.key).stat().st_mode)
    assert mode == 0o600


async def test_put_rejects_a_ref_whose_key_resolves_outside_the_store_root(tmp_path: Path) -> None:
    """`FileRef`'s own grammar (`domain/shared/files.py`) already makes a traversing key
    unrepresentable, so the only way to reach `LocalFileStore`'s belt-and-braces containment check
    (`_resolve_contained`) is to build a `FileRef` that never went through `__post_init__` at all —
    exactly the "a key built somewhere it should never have been built by hand" scenario
    `InvalidFileRef`'s docstring names as the only legitimate reason this check could ever fire.
    `object.__new__` skips `FileRef.__init__` (and therefore its validation) entirely;
    `object.__setattr__` is required next because the dataclass is frozen.
    """
    escaping_ref = object.__new__(FileRef)
    object.__setattr__(escaping_ref, "key", "../outside.pdf")

    store = LocalFileStore(tmp_path)

    with pytest.raises(RuntimeError, match="escaped the store root"):
        await store.put(escaping_ref, b"malicious")

    assert not (tmp_path.parent / "outside.pdf").exists()


async def test_put_translates_enospc_to_file_store_unavailable_without_logging_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F-14: `OSError(ENOSPC)` from the underlying filesystem call becomes `FileStoreUnavailable`,
    and the log line carries `errno` and the store root only — never the key, the final path, or the
    bytes (Constitution §8, the module's own docstring)."""
    configure_logging(Settings(app_env="test"))
    store = LocalFileStore(tmp_path)
    ref = _ref()

    def _raise_enospc(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("os.replace", _raise_enospc)

    with caplog.at_level(logging.INFO), pytest.raises(FileStoreUnavailable):
        await store.put(ref, b"content that must never be logged")

    log_output = caplog.text
    assert str(errno.ENOSPC) in log_output
    assert ref.key not in log_output
    assert "content that must never be logged" not in log_output


# --- `delete_partial`: the sibling of `put`'s temporary file (T18c) --------------------------------


async def test_delete_partial_removes_the_dot_part_sibling_and_leaves_the_final_key_alone(
    tmp_path: Path,
) -> None:
    """The fix for T18b's reclaim bug (see `domain/shared/files.py::FileStorePort.delete_partial`'s
    docstring): a `.part` and its final key are two different files on disk, so `delete_partial` must
    remove only the former. Written directly against `<key>.part` — built the same way
    `LocalFileStore._delete_partial_sync` does — rather than through `put`, since `put` always
    finishes its `os.replace` and never leaves a `.part` behind on a successful run."""
    store = LocalFileStore(tmp_path)
    ref = _ref()
    final_path = tmp_path / ref.key
    part_path = final_path.with_name(final_path.name + ".part")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"the live, referenced file")
    part_path.write_bytes(b"an abandoned partial write")

    await store.delete_partial(ref)

    assert not part_path.exists()
    assert final_path.exists()
    assert final_path.read_bytes() == b"the live, referenced file"


async def test_delete_partial_on_a_missing_dot_part_is_not_an_error(tmp_path: Path) -> None:
    """Mirrors `delete`'s `missing_ok=True` convention: another writer's `os.replace` may already
    have consumed the `.part` by the time the sweep gets to it, and that is the state the caller
    wanted, not a failure."""
    store = LocalFileStore(tmp_path)
    ref = _ref()  # never put, so neither the final key nor a `.part` exists

    await store.delete_partial(ref)  # must not raise


# --- `get`: the two-way failure split (X-47, X-48) -------------------------------------------------


async def test_get_on_an_absent_key_raises_stored_file_missing(tmp_path: Path) -> None:
    """X-47: a key that resolves to nothing is `StoredFileMissing`, the type the download handler
    needs to answer **410 `export_file_gone`** rather than **503** — the file will not come back, so
    retrying this exact request helps nobody; exporting again does. Asserts the *narrower* type
    directly (see the module docstring): `pytest.raises(FileStoreUnavailable)` alone would still
    pass if `LocalFileStore.get`'s two `except` clauses were swapped."""
    store = LocalFileStore(tmp_path)
    ref = _ref()  # never put

    with pytest.raises(StoredFileMissing):
        await store.get(ref)


async def test_get_on_an_unreadable_file_raises_file_store_unavailable_not_stored_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X-48: any other `OSError` — `EIO`, permissions, the volume unmounted — becomes
    `FileStoreUnavailable` and nothing narrower. Forced with a real, existing file whose *open* is
    made to fail through the module's own `_nofollow_opener` hook — `_get_sync` opens the descriptor
    through that opener rather than `Path.read_bytes` (the link-safety hardening: see the module
    docstring), so that hook is the one seam this failure can be forced through without mocking
    CPython's own buffered-IO internals. This is what proves the failure reaches `get`'s `OSError`
    floor rather than the `FileNotFoundError` branch for an unrelated reason (the ref not existing at
    all).

    `type(exc) is FileStoreUnavailable` — not merely `isinstance` — is the negative half of the
    module docstring's pair: `StoredFileMissing` being a subclass means an `isinstance` check here
    would still pass with the two `except` clauses swapped, exactly like the false-positive
    `pytest.raises(FileStoreUnavailable)` the sibling test above warns against.
    """
    store = LocalFileStore(tmp_path)
    ref = _ref()
    await store.put(ref, b"content")

    def _raise_eio(path: str, flags: int) -> int:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(local_file_store, "_nofollow_opener", _raise_eio)

    with pytest.raises(FileStoreUnavailable) as exc_info:
        await store.get(ref)

    assert type(exc_info.value) is FileStoreUnavailable
    assert not isinstance(exc_info.value, StoredFileMissing)


async def test_get_on_an_absent_key_logs_errno_and_root_but_never_the_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """X-47's log line: `file_store.get_missing` at `error`, carrying `errno` (`ENOENT` = 2) and
    `root` only — never the key or the resolved path, which carries a UUID that identifies one
    person's document (Constitution §8, ADR-0011 §6)."""
    configure_logging(Settings(app_env="test"))
    store = LocalFileStore(tmp_path)
    ref = _ref()

    with caplog.at_level(logging.INFO), pytest.raises(StoredFileMissing):
        await store.get(ref)

    log_output = caplog.text
    assert "file_store.get_missing" in log_output
    assert str(errno.ENOENT) in log_output
    assert str(tmp_path) in log_output
    assert ref.key not in log_output


async def test_get_on_an_unreadable_file_logs_errno_and_root_but_never_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """X-48's log line: `file_store.get_failed` at `error`, the same two fields, for any `OSError`
    that is not `FileNotFoundError`."""
    configure_logging(Settings(app_env="test"))
    store = LocalFileStore(tmp_path)
    ref = _ref()
    await store.put(ref, b"content")

    def _raise_eio(path: str, flags: int) -> int:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(local_file_store, "_nofollow_opener", _raise_eio)

    with caplog.at_level(logging.INFO), pytest.raises(FileStoreUnavailable):
        await store.get(ref)

    log_output = caplog.text
    assert "file_store.get_failed" in log_output
    assert str(errno.EIO) in log_output
    assert str(tmp_path) in log_output
    assert ref.key not in log_output


# --- `_resolve_contained`: a symlink planted at the final key (the T18b shape, second occurrence) --
#
# `test_put_rejects_a_ref_whose_key_resolves_outside_the_store_root` above pins the OTHER refusal
# `_resolve_contained` can raise — `RuntimeError`, for a key that escaped the store root, which
# `FileRef`'s own grammar makes unconstructable except by hand-building one. These tests are the
# *survivable* refusal: a real, syntactically valid `FileRef` whose final path component happens to
# be a symlink on disk, planted by something with prior write access to the volume (`api`/`worker`
# both mount it — the module's own honest threat model). `_resolve_contained` refuses it with
# `FileStoreUnavailable`, never `RuntimeError`, precisely so the orphan sweep counts one `failed` and
# the guest purge counts one `files_failed` and both continue (R-4, R-38) rather than the whole run
# dying over one planted link.
#
# **The assertion that matters most is not that the call raises — it is that the symlink's TARGET
# still exists with its original bytes afterwards.** Before `_resolve_contained` stopped resolving
# the final path component, `delete`/`get`/`put` all acted on whatever the link pointed at: a live
# file belonging to a *different* session, destroyed while the planted link itself survived and kept
# clearing the reference cross-check as "unreferenced" (T18b's shape, this time at the link layer
# rather than the `.part` layer).


def _plant_symlink_at(path: Path, *, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


async def test_delete_refuses_a_symlink_at_the_final_key_and_the_targets_bytes_survive(
    tmp_path: Path,
) -> None:
    victim_ref = _ref()
    link_ref = _ref()
    store = LocalFileStore(tmp_path)
    await store.put(victim_ref, b"a stranger's live, referenced file")
    victim_path = tmp_path / victim_ref.key
    link_path = tmp_path / link_ref.key
    _plant_symlink_at(link_path, target=victim_path)

    with pytest.raises(FileStoreUnavailable):
        await store.delete(link_ref)

    assert victim_path.exists(), "the symlink's target must survive the refused delete"
    assert victim_path.read_bytes() == b"a stranger's live, referenced file"
    assert link_path.is_symlink(), "the link itself must be untouched, not unlinked"


async def test_put_at_a_dot_part_symlink_fails_with_file_store_unavailable_and_the_targets_bytes_survive(
    tmp_path: Path,
) -> None:
    """The `_put_sync` half of the fix: `O_NOFOLLOW` on the `<key>.part` open turns a planted link
    there into `ELOOP`, translated to `FileStoreUnavailable` by `put`'s own `except OSError`. Before
    `O_NOFOLLOW`, a plain `open(part_path, "wb")` would have followed the link, written the new bytes
    onto the **victim's** file, and then `os.replace` would have installed the *link itself* at the
    real key — one upload silently overwriting a stranger's file elsewhere on the volume.
    """
    victim_path = tmp_path / "elsewhere" / "a-strangers-file.bin"
    victim_path.parent.mkdir(parents=True)
    victim_path.write_bytes(b"a stranger's live file, nowhere near this ref's own key")

    ref = _ref()
    final_path = tmp_path / ref.key
    part_path = final_path.with_name(final_path.name + ".part")
    _plant_symlink_at(part_path, target=victim_path)

    store = LocalFileStore(tmp_path)
    with pytest.raises(FileStoreUnavailable):
        await store.put(ref, b"an attacker's bytes, aimed at the victim through the .part link")

    assert victim_path.read_bytes() == b"a stranger's live file, nowhere near this ref's own key"
    assert part_path.is_symlink(), "the .part link itself must be untouched"
    assert not final_path.exists(), "the write must never have reached os.replace"


async def test_symlink_refusal_at_the_final_key_never_names_the_key_in_the_log_or_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R-37's rule applied one layer up: the key is the one thing that could name a person (it is
    this session's storage key, and a resolved path carries it), so it must not appear in either the
    raised exception's message or the log line — matching this file's own naming convention for every
    other failure here (`..._logs_errno_and_root_but_never_the_key`). The store's configured `root`
    IS expected in the log, as it already is on every sibling failure line in this module; only the
    key and the joined path are the control.
    """
    configure_logging(Settings(app_env="test"))
    victim_ref = _ref()
    link_ref = _ref()
    store = LocalFileStore(tmp_path)
    await store.put(victim_ref, b"irrelevant here")
    _plant_symlink_at(tmp_path / link_ref.key, target=tmp_path / victim_ref.key)

    with caplog.at_level(logging.INFO), pytest.raises(FileStoreUnavailable) as exc_info:
        await store.delete(link_ref)

    assert link_ref.key not in str(exc_info.value)
    assert link_ref.key not in caplog.text
    assert str(tmp_path / link_ref.key) not in caplog.text


# --- `get`'s own opener guard (`_nofollow_opener`) — the last of the four paths hardened, and the
# one every guest's export download takes ------------------------------------------------------------


async def test_get_refuses_a_symlink_at_the_final_key_via_the_opener_even_if_the_containment_check_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_resolve_contained` already refuses a symlink at the final key on its own — the `delete`-side
    test above exercises exactly that path — which makes the *naive* version of this test (plant a
    link, call `get`, assert `FileStoreUnavailable` and the target's bytes survive) pass whether or
    not `_get_sync` still passes `opener=_nofollow_opener` to `open()`, because `_resolve_contained`
    never lets the call reach `open()` in the first place. That would pin the outer check, the one
    `test_delete_refuses_a_symlink_at_the_final_key_and_the_targets_bytes_survive` above already
    covers, not the opener this test exists to cover. The module's own docstring calls
    `_resolve_contained` a *check-then-use* and the opener the second lock that closes the window
    between the check and the syscall — a window that exists precisely because a link can appear at
    the final key *after* the check has already passed.

    So this test stands in for that window directly: it monkeypatches `_resolve_contained` to hand
    back the symlink's own path unexamined, as if the link had appeared after the check ran, and
    asserts that `get` still refuses and the victim's bytes still survive. What is left standing
    between the link and the victim at that point is only the opener `_get_sync` passes to `open()`.

    Mutation-verified: with `opener=_nofollow_opener` removed from `_get_sync`'s `open()` call, this
    test reddens — the bypassed check lets `open()` follow the link and return the victim's own
    bytes, so `pytest.raises(FileStoreUnavailable)` fails with nothing raised. Restored byte-exact
    afterwards.
    """
    victim_ref = _ref()
    link_ref = _ref()
    store = LocalFileStore(tmp_path)
    await store.put(victim_ref, b"a stranger's live, referenced file")
    victim_path = tmp_path / victim_ref.key
    link_path = tmp_path / link_ref.key
    _plant_symlink_at(link_path, target=victim_path)

    def _bypass_containment_check(self: LocalFileStore, ref: FileRef) -> Path:
        return link_path

    monkeypatch.setattr(LocalFileStore, "_resolve_contained", _bypass_containment_check)

    with pytest.raises(FileStoreUnavailable):
        await store.get(link_ref)

    assert victim_path.read_bytes() == b"a stranger's live, referenced file"


async def test_get_translates_eloop_to_file_store_unavailable_not_stored_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error-split half of the opener guard (X-47/X-48, one layer up): on the opener path a
    symlink makes `open()` raise `ELOOP`, an `OSError` that is *not* `FileNotFoundError`, so it must
    reach `get`'s `OSError` floor and become `FileStoreUnavailable` — never `StoredFileMissing`,
    which would tell the download handler to answer 410 ("this will never come back") for a state
    that is, in fact, an operational failure worth a retry.

    **Corrected at `/verify` iteration 3, and the docstring was the thing that was wrong.** This test
    used to plant the link and call `get` directly, and its docstring claimed that made `open()`
    raise `ELOOP`. It does not: `_get_sync` evaluates `_resolve_contained(ref)` as the *argument* to
    `open()`, and that check refuses a link first — so the exception raised was the containment
    check's own `FileStoreUnavailable`, with `__cause__` of `None`, and no `OSError` was ever
    involved. The assertions were true, but they pinned the **outer check** for a third time while
    the docstring told a future reader the opener was covered. That is the more dangerous form of
    "a docblock claiming coverage the assertion cannot deliver": deleting `opener=_nofollow_opener`
    would have left a green suite *and* a comment vouching for it.

    It now bypasses containment exactly as the test above does, so the `ELOOP` it names is the one
    that actually happens, and the type split is asserted on the path where translation occurs.
    """
    victim_ref = _ref()
    link_ref = _ref()
    store = LocalFileStore(tmp_path)
    await store.put(victim_ref, b"irrelevant here")
    link_path = tmp_path / link_ref.key
    _plant_symlink_at(link_path, target=tmp_path / victim_ref.key)

    def _bypass_containment_check(self: LocalFileStore, ref: FileRef) -> Path:
        return link_path

    monkeypatch.setattr(LocalFileStore, "_resolve_contained", _bypass_containment_check)

    with pytest.raises(FileStoreUnavailable) as exc_info:
        await store.get(link_ref)

    # The translation really did come from an `OSError`, not from the containment check: `ELOOP`
    # (errno 40) is what `O_NOFOLLOW` raises on a symlink, and `get`'s floor re-raises `from exc`.
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.ELOOP
    assert type(exc_info.value) is FileStoreUnavailable
    assert not isinstance(exc_info.value, StoredFileMissing)
