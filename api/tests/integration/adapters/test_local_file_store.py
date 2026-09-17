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
    `FileStoreUnavailable` and nothing narrower. Forced with a real, existing file whose read is
    made to fail, so the only difference from the happy path is the read itself — this is what
    proves the failure reaches `get`'s `OSError` floor rather than the `FileNotFoundError` branch
    for an unrelated reason (the ref not existing at all).

    `type(exc) is FileStoreUnavailable` — not merely `isinstance` — is the negative half of the
    module docstring's pair: `StoredFileMissing` being a subclass means an `isinstance` check here
    would still pass with the two `except` clauses swapped, exactly like the false-positive
    `pytest.raises(FileStoreUnavailable)` the sibling test above warns against.
    """
    store = LocalFileStore(tmp_path)
    ref = _ref()
    await store.put(ref, b"content")

    def _raise_eio(*args: object, **kwargs: object) -> bytes:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(Path, "read_bytes", _raise_eio)

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

    def _raise_eio(*args: object, **kwargs: object) -> bytes:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(Path, "read_bytes", _raise_eio)

    with caplog.at_level(logging.INFO), pytest.raises(FileStoreUnavailable):
        await store.get(ref)

    log_output = caplog.text
    assert "file_store.get_failed" in log_output
    assert str(errno.EIO) in log_output
    assert str(tmp_path) in log_output
    assert ref.key not in log_output
