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

**The identical arrangement now holds for `export`, and it breaks the identical way.** Slice 1.5
adds `for_export`, so this module also imports `ExportDelivery`, `ExportFormat` and `ExportJobId`
from `tailorcraft.domain.export.value_objects` and `ExportFormatNotQueued` from
`tailorcraft.domain.export.errors` — while `export_job.py`, in that same package, imports `FileRef`
back out of here. The guarantee is the same one: `domain/export/__init__.py` re-exports nothing, and
neither `domain/export/value_objects.py` nor `domain/export/errors.py` imports this module, so none
of those imports can reach `export_job.py`. Add one re-export to that package `__init__` and the
cycle closes, surfacing as an `ImportError` at application startup rather than anywhere near the
edit that caused it. Two contexts now rest on this rule instead of one, which is the argument for
eventually giving
`for_base_cv` and `for_export` their own module — and the argument against doing it today is that
two keys derived from two ids is not yet a module's worth of behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from tailorcraft.domain.export.errors import ExportFormatNotQueued
from tailorcraft.domain.export.value_objects import ExportDelivery, ExportFormat, ExportJobId
from tailorcraft.domain.intake.errors import InvalidFileRef
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.errors import DomainError

# ADR-0011's grammar. A `fullmatch` against this pattern already rejects everything the failure
# contract lists by construction, with no separate checks needed: a leading `/` or a `..` segment
# cannot start with two hex digits followed by `/`; a backslash cannot appear where the grammar
# requires `/`; an embedded NUL (or anything else) after the extension breaks the trailing `$`
# anchor because `fullmatch` requires the *entire* string to match, not merely a prefix.
_KEY_GRAMMAR = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f-]{36}\.(pdf|docx|txt)$")


@dataclass(frozen=True, slots=True)
class FileRef:
    """An opaque storage key, not a path. Grammar (checked in `__post_init__`, not here):
    `^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f-]{36}\\.(pdf|docx|txt)$` — rejects `..`, a leading `/`, a
    backslash and any NUL, so a traversing key is unrepresentable rather than merely rejected later
    by `LocalFileStore`'s containment check (ADR-0011 §6 keeps that check anyway, as a second lock
    on a door this type already keeps shut)."""

    key: str

    def __post_init__(self) -> None:
        if not _KEY_GRAMMAR.fullmatch(self.key):
            raise InvalidFileRef(f"{self.key!r} does not match the storage grammar")

    @classmethod
    def for_base_cv(cls, cv_id: BaseCvId, content_type: CvContentType) -> FileRef:
        """Build `<hex[0:2]>/<hex[2:4]>/<uuid>.<ext>` from the id's own hex digits.

        Deterministic on purpose: same id, same key, every time. That is what makes a retried write
        idempotent (F-22) and what lets the row and the file find each other with no lookup table.
        Two-level hex sharding keeps any one directory small without a second piece of state to keep
        in step with the id (ADR-0011 §§1, 3).
        """
        hex_digits = cv_id.value.hex
        key = f"{hex_digits[0:2]}/{hex_digits[2:4]}/{cv_id.value}.{content_type.file_extension}"
        return cls(key=key)

    @classmethod
    def for_export(cls, job_id: ExportJobId, format: ExportFormat) -> FileRef:
        """Build `<hex[0:2]>/<hex[2:4]>/<uuid7>.<ext>` from an export job's own hex digits — the
        same grammar, the same sharding and the same determinism as `for_base_cv`, for output
        instead of input (AC-7, ADR-0016's amendment to ADR-0011).

        **The grammar is not widened.** `pdf` and `docx` are already admitted by `_KEY_GRAMMAR`,
        because 1.1 wrote it around the formats a CV arrives in and those happen to be the two a CV
        leaves in. `md` and `txt` are **never stored**: they render inline, inside the request, with
        no row, no worker and no file (ADR-0005, ADR-0016 (a)). Adding them to the pattern would
        admit a key for a file that nothing ever writes.

        **Raises `ExportFormatNotQueued` for an inline format**, rather than building a key the
        pattern would reject with the less informative `InvalidFileRef`. An inline format has no
        file and therefore no ref: the refusal is a statement about the delivery model, not about
        the string. It is also the same error `ExportJob.request` raises for the same rule (XJ-2),
        so a caller that somehow reached either one gets one answer.

        Deterministic on purpose: same job id and format, same key, every time. That is what makes a
        retried write idempotent, what lets `mark_ready` write the key with no argument to get wrong
        (XJ-7), and what lets 1.6 reconstruct every file's key from a row — or from an id alone —
        with no lookup table (AC-23). The filename is still a UUIDv7, so the orphan sweep needs no
        database at all (ADR-0011 §4); export files and base-CV files share the tree and the rule.

        The refusal asks `format.delivery`, never `format in (MD, TXT)`: the fact "this format has no
        file" is `ExportFormat`'s to answer (AC-1), and a membership test written out here would be a
        second copy of it that a fifth format could walk straight past.
        """
        if format.delivery is ExportDelivery.INLINE:
            raise ExportFormatNotQueued(format)
        hex_digits = job_id.value.hex
        key = f"{hex_digits[0:2]}/{hex_digits[2:4]}/{job_id.value}.{format.file_extension}"
        return cls(key=key)


class FileStorePort(Protocol):
    """The seam between the domain and wherever bytes actually live — a local volume today, object
    storage later, unchanged on this side of the port (ADR-0011's stated reason the layout is a key,
    not a path). No timeout, no retry count, no filesystem detail belongs in this signature; those
    are `LocalFileStore`'s business."""

    async def put(self, ref: FileRef, data: bytes) -> None: ...

    async def get(self, ref: FileRef) -> bytes: ...

    async def delete(self, ref: FileRef) -> None: ...

    async def delete_partial(self, ref: FileRef) -> None:
        """Remove the **incomplete write beside** `ref`'s key — never the key itself.

        A second method rather than `delete(ref, *, partial: bool)`, and the reason is the bug that
        produced it (slice 1.6, T18b). `put` writes `<key>.part`, fsyncs it and `os.replace`s it onto
        `<key>`; a crash in between leaves a `.part` for the orphan sweep. But **`FileRef`'s grammar
        cannot represent a `.part` name at all** — it ends `\\.(pdf|docx|txt)$` — so a partial is
        carried as *the base ref plus a separate flag*, and the sweep called plain `delete(ref)` for
        both. That unlinked the **final** key: the `.part` survived and was reported reclaimed, and
        where a live file sat at that key it was deleted instead, with the reference cross-check
        structurally unable to save it (partials are excluded from it by design).

        A boolean parameter would have fixed that case and left the shape that caused it: one call
        site, one flag threaded from a variable, and the wrong value silently deletes a stranger's
        document. Two names cannot be confused by a caller that forgot which way the flag pointed.

        Missing is not an error, exactly as for `delete`: a `.part` that another writer's
        `os.replace` already consumed leaves the world in the state the caller wanted.
        """
        ...


class FileStoreUnavailable(DomainError):
    """The store could not complete a read or write — disk full, permissions, filesystem gone. The
    adapter translates `OSError` into this; nothing above the port ever sees `errno`."""


class StoredFileMissing(FileStoreUnavailable):
    """The key resolves to nothing: the file a `ready` row names is not there (X-47).

    A **subclass**, not a sibling, and that is the whole design of this pair. `FileNotFoundError` is
    an `OSError`, so the existing floor in `LocalFileStore` already catches it and already keeps the
    port's promise — this is the *specific translation on top*, carrying the better reason, exactly
    as ADR-0012's obligation 10 describes: the floor makes the contract true by construction, the
    named translations make it informative. A caller that only cares "the store failed" still
    catches `FileStoreUnavailable` and needs no edit; the download handler catches this first and
    answers **410 `export_file_gone`** instead of 503, because a file that is gone will not come
    back and a retry of the *download* helps nobody — re-exporting does.

    Deleted by hand, a volume lost across a redeploy, or 1.6's retention sweep unlinking ahead of a
    cascade are the three ways it happens. `LocalFileStore.get` raises it on `FileNotFoundError`
    above the `OSError` floor; 1.1 added no caller of `get` at all, so nothing existing changes.

    Carries nothing, and above all **never the path**. A storage key is opaque by design (ADR-0011)
    and the resolved path names the volume layout of the box it ran on.
    """
