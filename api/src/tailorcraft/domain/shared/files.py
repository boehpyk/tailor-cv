"""Uploaded and rendered files, addressed by an opaque key derived from an aggregate id (ADR-0011).

`FileRef` lives here rather than in `intake` because `export` needs the identical concept in slice
1.5, and a second copy would drift. It stays pure — a validated string with a grammar, no `pathlib`,
no I/O — the path only exists inside `LocalFileStore` in `infrastructure/`.

A note on the direction of this dependency, because it looks backwards on first read: this module
imports `BaseCvId` and `CvContentType` from `tailorcraft.domain.intake`, even though `files.py` sits
in `shared`. That is intentional and it is not a Python import cycle — `domain/intake/__init__.py`
re-exports nothing (see its docstring), so importing `tailorcraft.domain.intake.value_objects` never
runs `base_cv.py`, which is the only module in `intake` that will import *this* one. If a future edit
adds a re-export to `domain/intake/__init__.py`, that guarantee breaks silently; keep that package
`__init__` free of re-exports, or give `for_base_cv` its own module instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.errors import DomainError


@dataclass(frozen=True, slots=True)
class FileRef:
    """An opaque storage key, not a path. Grammar (checked in `__post_init__`, not here):
    `^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f-]{36}\\.(pdf|docx|txt)$` — rejects `..`, a leading `/`, a
    backslash and any NUL, so a traversing key is unrepresentable rather than merely rejected later
    by `LocalFileStore`'s containment check (ADR-0011 §6 keeps that check anyway, as a second lock
    on a door this type already keeps shut)."""

    key: str

    def __post_init__(self) -> None:
        raise NotImplementedError

    @classmethod
    def for_base_cv(cls, cv_id: BaseCvId, content_type: CvContentType) -> FileRef:
        """Build `<hex[0:2]>/<hex[2:4]>/<uuid>.<ext>` from the id's own hex digits.

        Deterministic on purpose: same id, same key, every time. That is what makes a retried write
        idempotent (F-22) and what lets the row and the file find each other with no lookup table.
        Two-level hex sharding keeps any one directory small without a second piece of state to keep
        in step with the id (ADR-0011 §§1, 3).
        """
        raise NotImplementedError


class FileStorePort(Protocol):
    """The seam between the domain and wherever bytes actually live — a local volume today, object
    storage later, unchanged on this side of the port (ADR-0011's stated reason the layout is a key,
    not a path). No timeout, no retry count, no filesystem detail belongs in this signature; those
    are `LocalFileStore`'s business."""

    async def put(self, ref: FileRef, data: bytes) -> None: ...

    async def get(self, ref: FileRef) -> bytes: ...

    async def delete(self, ref: FileRef) -> None: ...


class FileStoreUnavailable(DomainError):
    """The store could not complete a read or write — disk full, permissions, filesystem gone. The
    adapter translates `OSError` into this; nothing above the port ever sees `errno`."""
