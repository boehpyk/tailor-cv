"""Request and response schemas for the `posting` bounded context.

This is the validation boundary (CLAUDE.md): these models **are** the API contract, not a
convenience wrapper around the domain. They are written against technical-plan.md's "API contract"
section field for field, so `qa`'s API tests have a real shape to assert against rather than one
invented while writing the handler.

`source` reuses the domain's own `PostingSource` enum rather than duplicating the closed set as a
second `StrEnum` here — the allowed direction of the dependency (ADR-0002: `infrastructure` may
import `domain`, never the reverse), and it means the wire values and the domain values cannot drift
apart by one being edited without the other.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tailorcraft.domain.posting.value_objects import PostingSource

# How much of a posting the list endpoint shows. A wire-format concern, computed at the boundary and
# deliberately not a domain concept — see `JobPostingSummary`.
PREVIEW_CHARACTERS = 280


class PastedJobPostingRequest(BaseModel):
    """`{"source": "pasted", "text": "..."}`.

    **`max_length` is 40,000 while the domain's ceiling is 30,000, and the gap is deliberate.** The
    schema's job is to refuse the absurd before anything parses it; the *domain's* job is to say what
    a job posting is. Setting both to 30,000 would mean a 30,001-character paste produced a Pydantic
    validation error — generic, and worded by a library — instead of `posting_text_too_long`, whose
    message names the real limit and tells the user what to do. The band between the two numbers is
    where the domain gets to answer.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["pasted"]
    text: str = Field(min_length=1, max_length=40_000)


class FetchedJobPostingRequest(BaseModel):
    """`{"source": "fetched", "url": "https://..."}`.

    `url` is a bounded `str` here, not a Pydantic `HttpUrl`. That looks like a missed opportunity and
    is not: `SourceUrl` (`domain/posting/value_objects.py`) is the type that decides what a
    job-posting URL may be — the scheme allow-list, the userinfo rule, the control-character rule —
    and it is the one the fetcher receives. Validating with `HttpUrl` here would put a *second,
    different* set of URL rules at the boundary, and the two would disagree the first time either
    changed. The schema bounds the length so an absurd string is refused before it is parsed; the
    value object decides everything else, and its `InvalidSourceUrl` is what the client sees.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["fetched"]
    url: str = Field(min_length=1, max_length=2_048)


CreateJobPostingRequest = Annotated[
    PastedJobPostingRequest | FetchedJobPostingRequest, Field(discriminator="source")
]
"""The tagged union on the wire — Pydantic v2's discriminated union, the counterpart to the `match`
over two frozen command dataclasses in `CaptureJobPosting`.

The discriminator is what makes `{"source": "pasted", "url": "..."}` and `{}` unrepresentable rather
than checked: Pydantic routes on `source` and then applies exactly one model's rules, so there is no
branch anywhere that has to decide what a body carrying both fields means. `extra="forbid"` on both
arms closes the other half — a `pasted` body carrying a `url` is a 422 rather than a silently
ignored field, which matters because silently ignoring it would fetch nothing while looking like it
had (ADR-0013)."""


class JobPostingResponse(BaseModel):
    """One `JobPosting`, in full, as the client sees it.

    **This carries the posting text, and that is a deliberate difference from `BaseCvResponse`**,
    which returns only a character count. A CV is dense PII the browser did not need in slice 1.1; a
    job posting is content the user is about to read and, in 1.4, edit in the browser — the client
    genuinely needs it. Two guards ride along with that decision: the *list* endpoint returns a
    preview rather than every posting's full text, and both `GET`s answer `Cache-Control: no-store`
    so a posting does not land in a shared cache or an intermediary's store.

    There is no `status` field, and the absence is the contract: a `JobPosting` that exists is
    complete (ADR-0013). A failed fetch produced no row and was answered as an HTTP error.

    `expires_at` is the **guest session's** expiry, not a property of the row — carrying it here
    makes the 24-hour retention promise visible in the payload as well as in the UI copy.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: PostingSource
    source_url: str | None
    title: str | None
    character_count: int
    text: str
    created_at: datetime
    expires_at: datetime


class JobPostingSummary(BaseModel):
    """One `JobPosting` as it appears in a list: everything except the full text.

    `preview` is the first `PREVIEW_CHARACTERS` characters, computed at the boundary in the router.
    It is a wire-format concern and deliberately not a domain concept — `JobPostingText` has no
    opinion about how much of itself a list should show.

    The reason is concrete rather than aesthetic: five postings at 30,000 characters is 150 KB of
    user content in one response, and in whatever caches it. The detail endpoint is there for the
    one posting the user actually opened.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: PostingSource
    source_url: str | None
    title: str | None
    character_count: int
    preview: str
    created_at: datetime
    expires_at: datetime


class JobPostingListResponse(BaseModel):
    """Every `JobPosting` a guest session owns. `items` is `[]` for a session with none — an empty
    list is an ordinary answer to `GET /api/job-postings`, never a 404."""

    model_config = ConfigDict(from_attributes=True)

    items: list[JobPostingSummary] = Field(default_factory=list)
