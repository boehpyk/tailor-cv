"""Adapter tests for `LocalFileStore` (`FileStorePort`), written **after** (T29): atomic replace,
file mode, path-containment rejection, and the `ENOSPC` → `FileStoreUnavailable` translation
(technical-plan.md's Adapters row; F-14).

No database and no `clear_redis` needed — this adapter's whole world is a local directory, given
fresh per test via `tmp_path`.
"""

from __future__ import annotations

import errno
import logging
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
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
