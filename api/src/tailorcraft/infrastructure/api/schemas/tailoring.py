"""Request and response schemas for the `tailoring` bounded context — the `TailoringRun` wire format.

This is the validation boundary (CLAUDE.md): these models **are** the API contract, not a
convenience wrapper around the domain. They are written against
`docs/specs/tailoring-generate-documents/technical-plan.md`'s "API contract" section field for
field, so `qa`'s API tests (T30) have a real shape to assert against rather than one invented while
writing the handler.

`status` and `failure_reason` reuse the domain's own `TailoringRunStatus` /
`TailoringFailureReason` enums rather than duplicating the same closed sets as second `StrEnum`s
here — the allowed direction of the dependency (ADR-0002: `infrastructure` may import `domain`,
never the reverse), and it means the wire values and the domain values cannot drift apart by one
being edited without the other. `intake` and `posting` made the same call for the same reason.

**Three deliberate choices in this payload**, each carried at the field that embodies it:

1. **`retryable` is computed at the boundary from `failure_reason`** — a business rule, and the API
   is its authority (AC-13).
2. **`prompt_tokens` and `completion_tokens` are NOT here** — cost accounting, not something a user
   can act on.
3. **The list omits both document bodies** — half a megabyte of a stranger's PII per response
   otherwise.

`TailoringRunSummary` is **not** a subclass of `TailoringRunResponse` and `TailoringRunResponse` is
not a subclass of it. Shared shape is not shared meaning (CLAUDE.md): one of these two is "the run
you opened, in full" and the other is "one row of a list that must never carry a document", and a
base class would make the *next* field default to appearing in both — which is exactly the direction
that leaks. The duplication is six lines and it is the cheap half of the trade. `posting` split
`JobPostingResponse` / `JobPostingSummary` the same way.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason, TailoringRunStatus


class CreateTailoringRunRequest(BaseModel):
    """`{"base_cv_id": "...", "job_posting_id": "..."}` — the whole request body.

    Both ids are `UUID`-typed, so a malformed one is FastAPI's own 422 `validation_error` (G-2)
    rather than something the handler rejects by hand. `extra="forbid"` closes the other half: a
    body carrying a stray field is a 422 rather than a silently ignored key, matching both arms of
    1.2's `CreateJobPostingRequest`.

    There is no `model`, no `prompt_version`, no temperature and no token budget on this request, and
    the absence is the contract rather than an omission: **what the client may ask for is "tailor
    this CV against this posting"**, and nothing about how we pay for it. Every one of those knobs is
    configuration read by the adapter (ADR-0004), and a caller-supplied one would be a caller-chosen
    bill.

    Note what is *not* validated here either: that the two ids exist, and that they belong to the
    caller. Both are authorization questions about the link between an object and a session, they are
    answered **in the use case** on every call (AC-14), and answering them at the boundary would put
    a second copy of the rule somewhere a future endpoint can forget to apply.
    """

    model_config = ConfigDict(extra="forbid")

    base_cv_id: UUID
    job_posting_id: UUID


class TailoringRunResponse(BaseModel):
    """One `TailoringRun`, in full, as the client sees it — including both tailored documents.

    **This carries the tailored CV and the cover letter, and that is the whole point of the
    endpoint**: the 1.4 editor loads them into TipTap. It is also why both `GET`s answer
    `Cache-Control: no-store` — this body is a person's rewritten employment history plus a letter
    naming the employer they are applying to, which is *sharper* than the upload it came from.

    Every field after `retryable` is `None` while the run is `queued` or `running`, and that is the
    polling contract (AC-1): a 202 hands back this same shape with `tailored_cv`, `cover_letter`,
    `model` and `completed_at` all `null`, and the client re-reads it until `status` is terminal.

    **`prompt_tokens` and `completion_tokens` are deliberately absent.** They are recorded on the row
    and in the log line, because they are how the cost of this feature is watched — but they are cost
    accounting, and a user cannot act on them. Naming the absence here is what stops a later reader
    from "completing" the payload from the columns.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: TailoringRunStatus
    base_cv_id: UUID
    job_posting_id: UUID

    failure_reason: TailoringFailureReason | None

    # **Computed at the boundary from `failure_reason`, never stored and never re-derived by the
    # client** (AC-13, Constitution §4.5). "Which failures are worth paying for again" is a business
    # rule: `llm_refused` and `inputs_too_large` will answer identically to a second identical call,
    # so offering "Try again" for them would be selling the same refusal twice. A TypeScript copy of
    # that rule is a second authority that drifts, so the API is the only one — the handler computes
    # it with a `match` over `TailoringFailureReason` closed by `assert_never` (T31), which makes a
    # reason added later a type error here rather than a silently-missing button in the browser.
    #
    # `False` for a run that has not failed, which is not a claim that it is unretryable — a
    # `queued`, `running` or `succeeded` run has nothing to retry, and the client only reads this
    # field when `status == "failed"`.
    retryable: bool

    tailored_cv: str | None
    cover_letter: str | None
    tailored_cv_character_count: int | None
    cover_letter_character_count: int | None

    # Provenance, not configuration (see `ModelName`): what actually produced these words, recorded
    # at the moment of the call. `None` until the call succeeds.
    model: str | None
    prompt_version: str | None
    llm_duration_ms: int | None

    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    # The **guest session's** expiry, not a property of the run row — the session owns the 24-hour
    # promise (ADR-0006) and carrying it here puts that promise in the payload as well as in the UI
    # copy, exactly as `BaseCvResponse` and `JobPostingResponse` do.
    expires_at: datetime


class TailoringRunSummary(BaseModel):
    """One `TailoringRun` as it appears in a list: everything above **except the two documents**.

    The character counts stay, the bodies do not, and that is a privacy decision with a number behind
    it rather than a tidiness one. A session may own 20 runs (`max_tailoring_runs_per_session`) and a
    tailored CV may reach 20,000 characters with a cover letter behind it — over half a megabyte of a
    stranger's rewritten employment history in a single response, and in whatever caches, proxies or
    browser stores touch it. The detail endpoint exists for the one run the user actually opened.

    The same guard 1.2 put on `JobPostingSummary.preview`, with a bigger number behind it — and
    without even a preview here, because the first 280 characters of a tailored CV are someone's name
    and address.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: TailoringRunStatus
    base_cv_id: UUID
    job_posting_id: UUID

    failure_reason: TailoringFailureReason | None
    retryable: bool

    tailored_cv_character_count: int | None
    cover_letter_character_count: int | None

    model: str | None
    prompt_version: str | None
    llm_duration_ms: int | None

    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime


class TailoringRunListResponse(BaseModel):
    """Every `TailoringRun` a guest session owns, newest first. `items` is `[]` for a session with
    none — an empty list is an ordinary answer to `GET /api/tailoring-runs`, never a 404."""

    model_config = ConfigDict(from_attributes=True)

    items: list[TailoringRunSummary] = Field(default_factory=list)
