"""Request and response schemas for the `tracking` bounded context — the application board's wire
format (slice 3.1, technical plan §4).

This is the validation boundary (CLAUDE.md): these models **are** the HTTP contract, written field
for field against §4 at the skeleton step (T20) so `qa`'s API tests (T21) assert against a real
shape rather than one invented while writing the handlers.

**Four deliberate choices**, each carried at the field that embodies it:

1. **`stage` is the domain's own `ApplicationStage`**, not a second `Literal` of the six values.
   The plan's §3 phrase is `Literal[…six…]`; the wire contract is identical either way (an
   OpenAPI `enum` of the six strings, anything else a 422), and reusing the enum is the call
   `tailoring`'s, `intake`'s and `posting`'s schemas make for the same reason — the wire values and
   the domain values cannot drift apart by one being edited without the other (ADR-0002 allows the
   direction: `infrastructure` imports `domain`).
2. **`title` carries no `max_length`** (plan §3). The 120-character rule is `ApplicationTitle`'s,
   enforced when the handler builds the value object and translated to 422 `validation_error` by
   `api/errors.py`. A Pydantic bound would duplicate the rule *and* render `input_value` — the
   title — into a `ValidationError` (T-16: the title is never echoed). `JSON_REQUEST_MAX_BYTES`
   still caps the body as everywhere.
3. **`extra="forbid"` on every request** — an unknown key is a 422 (AC-25), not silently dropped.
4. **`BoardCardResponse` is not a subclass of `TrackedApplicationResponse`**, though §4 describes
   it as "TrackedApplication + three nullable joins". Shared shape is not shared meaning
   (CLAUDE.md): one is the card a write returns, the other a row of a read model, and a base class
   would make the *next* field on either default to appearing in both. Seven duplicated lines are
   the cheap half of that trade — `TailoringRunSummary` and `TailoringRunResponse` made the same one.

The board's nested ids are named for what they are (`job_posting_id`, `base_cv_id`), as §4 spells
them — unlike history's `HistoryPostingResponse.id`, because on a card the bare `id` is the card's.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tailorcraft.domain.posting.value_objects import PostingSource
from tailorcraft.domain.tracking.value_objects import ApplicationStage

# --------------------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------------------


class TrackApplicationRequest(BaseModel):
    """`POST /api/me/tracked-applications` — put a succeeded run on the board.

    `stage` defaults to `to_apply`; `title` is optional and nullable (no title is a card that shows
    its posting's)."""

    model_config = ConfigDict(extra="forbid")

    tailoring_run_id: UUID
    stage: ApplicationStage = ApplicationStage.TO_APPLY
    # No `max_length`: the module docstring's point 2.
    title: str | None = None


class MoveTrackedApplicationRequest(BaseModel):
    """`PUT /api/me/tracked-applications/{id}/stage` — move a card to another column, against the
    `version` the client last saw (ADR-0015's optimistic concurrency)."""

    model_config = ConfigDict(extra="forbid")

    stage: ApplicationStage
    version: int = Field(ge=1)


class RetitleTrackedApplicationRequest(BaseModel):
    """`PUT /api/me/tracked-applications/{id}/title` — set or clear a card's title.

    `title` is **required and nullable**: `null` clears it, and a missing key is a 422 rather than a
    silent clear."""

    model_config = ConfigDict(extra="forbid")

    # Required (no default), nullable. No `max_length`: the module docstring's point 2.
    title: str | None
    version: int = Field(ge=1)


# --------------------------------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------------------------------


class TrackedApplicationResponse(BaseModel):
    """A card, as a write returns it (`POST` 201, both `PUT`s 200): exactly these seven keys
    (AC-21)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    tailoring_run_id: UUID
    stage: ApplicationStage
    title: str | None
    tracked_at: datetime
    stage_changed_at: datetime
    version: int


class BoardRunResponse(BaseModel):
    """The run a card refers to: when it was requested and whether its documents were edited."""

    model_config = ConfigDict(extra="forbid")

    requested_at: datetime
    edited: bool


class BoardPostingResponse(BaseModel):
    """The posting a card's run was tailored to. `preview` is at most 140 characters, cut in SQL;
    the full text is never on the board (AC-31)."""

    model_config = ConfigDict(extra="forbid")

    job_posting_id: UUID
    source: PostingSource
    title: str | None
    source_url: str | None
    preview: str


class BoardBaseCvResponse(BaseModel):
    """The saved CV a card's run started from — `null` on the card once that CV is deleted (T-25)."""

    model_config = ConfigDict(extra="forbid")

    base_cv_id: UUID
    label: str | None
    original_filename: str


class BoardCardResponse(BaseModel):
    """One card on the board: the card's seven fields plus three nullable joins (AC-30).

    `run` is `null` (and `posting` with it) only for a card whose run row is missing — prevented by
    AC-18's locks, still listed so it stays removable (T-36). Not a `TrackedApplicationResponse`
    subclass: the module docstring's point 4."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    tailoring_run_id: UUID
    stage: ApplicationStage
    title: str | None
    tracked_at: datetime
    stage_changed_at: datetime
    version: int
    run: BoardRunResponse | None
    posting: BoardPostingResponse | None
    base_cv: BoardBaseCvResponse | None


class BoardResponse(BaseModel):
    """`GET /api/me/board`: every card of the user, `stage_changed_at DESC, id DESC`, unpaginated
    (ADR-0024 amendment (a)). An empty board is `{"items": []}` and a 200."""

    model_config = ConfigDict(extra="forbid")

    items: list[BoardCardResponse]
