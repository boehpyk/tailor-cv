"""`TypeDecorator`s for the `identity` context's value objects (ADR-0007)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.identity.value_objects import GuestSessionId


class GuestSessionIdType(TypeDecorator[GuestSessionId]):
    """`identity_guest_session.id` and `intake_base_cv.guest_session_id` — a native Postgres `UUID`
    carrying a `GuestSessionId` rather than a bare `UUID`, so a query result rehydrates the typed id
    the domain expects instead of handing application code a primitive to re-wrap by hand."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: GuestSessionId | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> GuestSessionId | None:
        if value is None:
            return None
        return GuestSessionId(value)
