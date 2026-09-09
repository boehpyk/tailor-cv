"""Response schemas for the `intake` bounded context — the `BaseCv` wire format.

This is the validation boundary (CLAUDE.md): these models are the API contract itself, not a
convenience wrapper around the domain. They are written against
`docs/specs/intake-base-cv-upload/technical-plan.md`'s "API contract" section, field for field, so
that `qa`'s API tests (T25) have a real shape to assert against rather than one invented while
writing the handler.

`status` and `failure_reason` reuse the domain's own `BaseCvStatus` / `ExtractionFailureReason`
enums directly (`domain/intake/value_objects.py`) rather than duplicating the same closed set as a
second `StrEnum` here. That is the allowed direction of the dependency (ADR-0002: `infrastructure`
may import `domain`, never the reverse) and it means the wire values and the domain values cannot
drift apart by one being edited without the other.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tailorcraft.domain.intake.value_objects import (
    BaseCvStatus,
    CvContentType,
    ExtractionFailureReason,
)


class BaseCvResponse(BaseModel):
    """One `BaseCv`, as the client sees it.

    **`extracted_text` is deliberately not a field.** The API returns a character count instead
    (`character_count`) — every byte of CV text that crosses the wire is a byte that can land in a
    browser cache, a CDN, or an intermediary's access log, and nothing on the client needs the text
    itself in this slice (feature-spec.md, "No returning extracted text over the wire").

    `failure_message` is the user-facing sentence for `failure_reason`, generated server-side so the
    client never re-implements the mapping from a reason code to a sentence a user should read.

    `expires_at` is the *guest session's* expiry, not a property of the CV row itself — carrying it
    here makes the 24-hour retention promise visible in the payload as well as in the UI copy
    (AC-16, ADR-0006 §5).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_filename: str
    content_type: CvContentType
    size_bytes: int
    status: BaseCvStatus
    character_count: int | None
    failure_reason: ExtractionFailureReason | None
    failure_message: str | None
    uploaded_at: datetime
    expires_at: datetime


class BaseCvListResponse(BaseModel):
    """Every `BaseCv` a guest session owns. `items` is `[]` for a session with none — an empty list
    is a perfectly ordinary answer to `GET /api/base-cvs`, never a 404 (AC-9's GET contract)."""

    model_config = ConfigDict(from_attributes=True)

    items: list[BaseCvResponse] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    """The one shape every error this API returns takes: a stable machine-readable `code` the
    client can branch on, and a `message` meant for a human. `code` is the contract; `message` is
    not — a client must never parse prose to decide what happened."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """The error envelope: `{"error": {"code": "...", "message": "..."}}`. Every non-2xx response
    from this router uses this shape, produced by a `DomainError` -> status/code mapping in the
    router (the domain itself never knows an HTTP status code exists)."""

    error: ErrorDetail
