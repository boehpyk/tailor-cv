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
    BaseCvOrigin,
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

    `origin` (slice 2.2, additive): `uploaded`, or `copied_from_saved` for a working copy made from a
    registered user's saved CV — the client's cue for the *Working copy* badge (AC-41). Derived from
    the aggregate (`BaseCv.origin`), never stored. **Nothing creates a working copy since 2.4**
    (the copy route was retired, ADR-0022 amendment (d)); this field is a *reader* kept until none
    can exist — see the contraction trigger on `origin` below.
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
    # Contraction trigger (2.4 technical plan §0.9, ADR-0022 amendment (d)): the first slice that
    # migrates `intake_base_cv` after 2.4's release + 24 h — or any time after, on a production read
    # showing zero rows with `copied_from_base_cv_id IS NOT NULL` — drops this field together with
    # the column, `BaseCv.copied_from`, `BaseCv.origin`, `BaseCvOrigin`, the *Working copy* badge
    # and the claim's `copied_from_base_cv_id IS NULL` clause, in one contract migration.
    origin: BaseCvOrigin


class BaseCvListResponse(BaseModel):
    """Every `BaseCv` a guest session owns. `items` is `[]` for a session with none — an empty list
    is a perfectly ordinary answer to `GET /api/base-cvs`, never a 404 (AC-9's GET contract)."""

    model_config = ConfigDict(from_attributes=True)

    items: list[BaseCvResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Saved base CVs — slice 2.2 (technical plan §4). `/api/me/base-cvs*`.
# ---------------------------------------------------------------------------------------------

LABEL_WIRE_MAX_LENGTH = 200
"""What `RenameSavedBaseCvRequest` will *parse*: looser than `BaseCvLabel`'s 80 on purpose (technical
plan §4). The schema bounds what we are willing to read; the value object says what is valid, with a
message the user can act on — 2.1's `password ≤ 1024` pattern. A 120-character label is therefore
422 `invalid_label` from the domain, not a generic `validation_error` from here."""


class SavedBaseCvResponse(BaseModel):
    """One saved base CV, as its owner sees it — `GET`/`POST /api/me/base-cvs`, `PATCH …/{id}`.

    `BaseCvResponse`'s fields minus `expires_at`, plus `label`. **No `expires_at`**: a saved CV
    never expires, and a field that could only ever be `null` would read as "unknown" rather than
    "never". **No text and no bytes** — `character_count` is computed from the text (for the list,
    by Postgres, without selecting it: `SavedBaseCvSummary`), never the text itself. No `origin`: a
    saved CV is only ever uploaded; 2.2's working copies lived in the guest workspace, and since
    2.4 nothing makes one.

    Built from a `SavedBaseCvSummary` on the list, and from the `BaseCv` aggregate after an upload
    or a rename — two sources, one key set, which is exactly what AC-20 pins.
    """

    id: UUID
    label: str | None
    original_filename: str
    content_type: CvContentType
    size_bytes: int
    status: BaseCvStatus
    character_count: int | None
    failure_reason: ExtractionFailureReason | None
    failure_message: str | None
    uploaded_at: datetime


class SavedBaseCvListResponse(BaseModel):
    """`GET /api/me/base-cvs` — newest first. `items` is `[]` for a user with none (S-13), never a
    404."""

    items: list[SavedBaseCvResponse] = Field(default_factory=list)


class RenameSavedBaseCvRequest(BaseModel):
    """`PATCH /api/me/base-cvs/{id}` — `{"label": str | null}`; `null` clears the label.

    The key is **required**: a body of `{}` is a 422 `validation_error`, not a silent "clear". A
    rename that means "clear" says so with an explicit `null`."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(max_length=LABEL_WIRE_MAX_LENGTH)


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
