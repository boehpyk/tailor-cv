"""`TypeDecorator`s round-tripping the `tracking` context's value objects (ADR-0007, slice 3.1).

One class per value object, written out, as in every other `types/` module. Four of them: the plan
(§3) names three — `TrackedApplicationIdType`, `ApplicationStageType`, `ApplicationTitleType` — and
`TrackedRunRefType` is the fourth the mapping needs, because `tailoring_run_id` holds tracking's own
`TrackedRunRef` rather than tailoring's `TailoringRunId` (`domain/tracking` imports no sibling context,
ADR-0029 decision 1). Lending `TailoringRunIdType` to that column would load a `TailoringRunId` into
an attribute annotated `TrackedRunRef`, and the aggregate's `==` against a `TrackedRunRef` would then
be `False` for the right run — the typed-id argument cutting the wrong way.

**`ApplicationTitleType` re-validates on load**, as `BaseCvLabelType` does: `ApplicationTitle(...)`
runs its `__post_init__` on every read. That is cheap (≤ 120 characters) and it means a title a
hand-written `UPDATE` slipped past `ck_tracking_application_title_length` — a control character, say,
which the CHECK does not look for — fails loudly on load instead of reaching a page. A title is user
text: nothing here logs it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    ApplicationTitle,
    TrackedApplicationId,
    TrackedRunRef,
)


class TrackedApplicationIdType(TypeDecorator[TrackedApplicationId]):
    """`tracking_application.id` — a native `UUID` carrying a `TrackedApplicationId`."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(
        self, value: TrackedApplicationId | None, dialect: Dialect
    ) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(
        self, value: Any | None, dialect: Dialect
    ) -> TrackedApplicationId | None:
        if value is None:
            return None
        return TrackedApplicationId(value)


class TrackedRunRefType(TypeDecorator[TrackedRunRef]):
    """`tracking_application.tailoring_run_id` — a native `UUID` carrying tracking's `TrackedRunRef`.

    The column has **no foreign key** to `tailoring_run` (ADR-0029, plan §0.7); the module docstring
    says why this is not `TailoringRunIdType`.
    """

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: TrackedRunRef | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> TrackedRunRef | None:
        if value is None:
            return None
        return TrackedRunRef(value)


class ApplicationStageType(TypeDecorator[ApplicationStage]):
    """`tracking_application.stage` — `VARCHAR(16)` plus `ck_tracking_application_stage_known`, not a
    native Postgres `ENUM` (adding a member to one takes a lock; `TailoringRunStatusType`'s reason).

    Loads an actual `ApplicationStage` member, not a bare `str` that compares equal: the board groups
    by member, and `is` against a member is `False` for the string.
    """

    impl = String(16)
    cache_ok = True

    def process_bind_param(self, value: ApplicationStage | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ApplicationStage | None:
        if value is None:
            return None
        return ApplicationStage(value)


class ApplicationTitleType(TypeDecorator[ApplicationTitle]):
    """`tracking_application.title` — `TEXT NULL`. `NULL` is the ordinary case (most cards carry no
    title of their own), so the `None` guard on the way out is load-bearing, not boilerplate.

    The length bound lives in the value object and in `ck_tracking_application_title_length`, not in
    the column type: `TEXT` rather than `VARCHAR(120)`, because Postgres's `VARCHAR(n)` counts
    characters exactly as `char_length` does, and one rule stated as a named CHECK is easier to find
    and to change than a type modifier.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: ApplicationTitle | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ApplicationTitle | None:
        if value is None:
            return None
        return ApplicationTitle(value)
