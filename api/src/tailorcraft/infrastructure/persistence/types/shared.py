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
    """`intake_base_cv.file_key` — `VARCHAR(512)`, never `NULL` (I-1: exactly one `FileRef`).

    512 rather than the grammar's ~45-character minimum length because ADR-0011's layout is a
    contract other contexts (`export`) will also address through, and headroom here is cheaper than
    a migration later.
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
