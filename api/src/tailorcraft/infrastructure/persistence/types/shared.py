"""`TypeDecorator`s for value objects that live in `domain/shared/` (ADR-0007).

`FileRef` is declared in `domain/shared/files.py` rather than `intake` because `export` needs the
identical concept in slice 1.5 (see that module's docstring) — its `TypeDecorator` follows it here
for the same reason, rather than living under `types/intake/`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.shared.files import FileRef


class FileRefType(TypeDecorator[FileRef]):
    """`intake_base_cv.file_key` and `export_job.file_key` — `VARCHAR(512)`.

    512 rather than the grammar's ~45-character minimum length because ADR-0011's layout is a
    contract other contexts (`export`) will also address through, and headroom here is cheaper than
    a migration later. **Slice 1.5 is that other context**, and it reuses this class rather than
    declaring its own: one value object, one decorator, so the storage grammar cannot be
    re-validated by two different rules on the way out of two tables.

    **The two columns differ in nullability, and the decorator is indifferent to that** — `NOT NULL`
    on `intake_base_cv` (I-1: a base CV has exactly one `FileRef` from the moment it exists) and
    `NULL` on `export_job` until the render succeeds (XJ-3, where
    `ck_export_job_file_key_matches_status` is what actually ties the key to `status = 'ready'`).
    Nullability is the column's declaration, never the type's, which is why the `None` guard runs in
    both directions here even though 1.1's only caller could never hit it.

    A note on the width, because the technical plan's column table for `export_job` says
    `VARCHAR(64)` and this ships `VARCHAR(512)`: reusing the decorator won, deliberately. A second
    class differing from this one only in a length is precisely the duplication the paragraph above
    rules out, and the 512 was chosen *for* this reuse (read the sentence 1.1 wrote — it names
    `export`). The plan's 64 was a tight bound on a ~47-character key; nothing depends on it, and a
    key too long for the grammar is unconstructable before it ever reaches a column.
    """

    impl = String(512)
    # Stateless: `process_bind_param` / `process_result_value` depend on nothing but the value
    # passed in, so the compiled statement is safe to cache across calls.
    cache_ok = True

    def process_bind_param(self, value: FileRef | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.key

    def process_result_value(self, value: Any | None, dialect: Dialect) -> FileRef | None:
        if value is None:
            return None
        return FileRef(key=value)
