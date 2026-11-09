"""`TypeDecorator`s round-tripping the `billing` context's value objects (ADR-0007, slice 5.2).

One class per value object, written out, as in every other `types/` module. `ChargedRunRefType` loads
billing's own `ChargedRunRef`, never tailoring's `TailoringRunId` (`TrackedRunRefType`'s reason): the
aggregate's `==` against a `ChargedRunRef` must be `True` for the right run.

`CreditEntryKindType` is `VARCHAR(24)` plus `ck_billing_credit_entry_kind_known`, not a native `ENUM`
(adding a member to one takes a lock; `TailoringRunStatusType`'s reason), and it loads a member, not a
bare `str`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import String
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.billing.value_objects import ChargedRunRef, CreditEntryId, CreditEntryKind


class CreditEntryIdType(TypeDecorator[CreditEntryId]):
    """`billing_credit_entry.id` — a native `UUID` carrying a `CreditEntryId`."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: CreditEntryId | None, dialect: Dialect) -> UUID | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> CreditEntryId | None:
        return None if value is None else CreditEntryId(value)


class ChargedRunRefType(TypeDecorator[ChargedRunRef]):
    """`billing_credit_entry.tailoring_run_id` — a native `UUID`, no foreign key (ADR-0014)."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: ChargedRunRef | None, dialect: Dialect) -> UUID | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ChargedRunRef | None:
        return None if value is None else ChargedRunRef(value)


class CreditEntryKindType(TypeDecorator[CreditEntryKind]):
    """`billing_credit_entry.kind` — `VARCHAR(24)`."""

    impl = String(24)
    cache_ok = True

    def process_bind_param(self, value: CreditEntryKind | None, dialect: Dialect) -> str | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> CreditEntryKind | None:
        return None if value is None else CreditEntryKind(value)
