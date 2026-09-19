"""Request and response schemas for the `export` bounded context — the `ExportJob` wire format.

This is the validation boundary (CLAUDE.md): these models **are** the API contract, not a
convenience wrapper around the domain. They are written against
`docs/specs/export-multi-format-download/technical-plan.md`'s "API contract" section field for
field, so `qa`'s API tests (I17) have a real shape to assert against rather than one invented while
writing the handler.

**Written whole at the SKELETON stage, deliberately** — the same call T1 made for
`domain/export/events.py` and I1 made for the pipeline's constants. A Pydantic field list *is* its
signature: there is no body to stub, nothing to "implement later", and a `NotImplementedError`
cannot live inside a field declaration. So the tests I17 writes over the *shape* of these models are
green the moment they arrive, and that is correct rather than a missed red. What is genuinely
red-first here is the router — status codes, headers, the failure-contract rows — and that is what
`routers/export.py` leaves raising.

`document`, `format`, `status` and `failure_reason` reuse the domain's own enums rather than
duplicating the same closed sets as second `StrEnum`s here — the allowed direction of the dependency
(ADR-0002: `infrastructure` may import `domain`, never the reverse), and it means the wire values
and the domain values cannot drift apart by one being edited without the other. `tailoring`,
`intake` and `posting` all made the same call for the same reason.

**None of these three models sets `from_attributes`, and the absence is the design.** A
`TailoringRunResponse` can be validated straight off an aggregate; an `ExportJobResponse` cannot,
because three of its sixteen fields — `retryable`, `current` and `file_url` — exist on no aggregate
at all. They are computed at the boundary (see `ExportJobResponse`), so every instance of this model
is built by keyword from a job plus the run's current version, and a `ConfigDict` enabling a
construction path nothing uses would be a promise this module does not keep.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobStatus,
)
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind


class CreateExportRequest(BaseModel):
    """`{"document": "cv" | "cover_letter", "format": "pdf" | "docx"}` — the whole request body of
    `POST /api/tailoring-runs/{run_id}/exports`.

    **Which run** is in the URL, not here, exactly as 1.4's `ReviseDocumentRequest` keeps `kind` in
    the path: the run is the resource this collection hangs off, and a run id in the body would be a
    second copy of it free to disagree with the first.

    `format` is `Literal["pdf", "docx"]` and **not** `ExportFormat`. That is the first of this
    rule's three locks (X-10): the queued/inline split is unrepresentable on this endpoint rather
    than rejected inside a handler, so `{"format": "md"}` is FastAPI's own 422 `validation_error`
    before a line of ours runs (AC-15). The second lock is `ExportJob.request`, which raises
    `ExportFormatNotQueued` for an inline member whatever the entry point (X-15); the third is the
    `CHECK (format IN ('pdf','docx'))` on the row. Three locks, because the failure this rule
    prevents is WeasyPrint on the event loop — which passes every test with one user and collapses
    at five.

    A `Literal` rather than a narrowed enum because there is no `QueuedExportFormat` type in the
    domain and inventing one here would be a second closed set to keep in step with `ExportFormat`.
    The router widens the literal back to the domain enum in one place.

    `document` *is* the domain enum, because the boundary needs no subset of it: both kinds are
    exportable in both queued formats.

    `extra="forbid"` closes the other half — a body carrying a stray field is a 422 rather than a
    silently ignored key (X-10), matching `CreateTailoringRunRequest` and `ReviseDocumentRequest`.

    Note what is *not* validated here: that the run exists, that it belongs to the caller, and that
    it is `succeeded`. The first two are authorization questions about the link between an object
    and a session, answered **in the use case** on every call (AC-14); the third is a question about
    state that only the loaded aggregate can answer. Answering any of them at the boundary would put
    a second copy of the rule somewhere a future endpoint can forget to apply.
    """

    model_config = ConfigDict(extra="forbid")

    document: TailoredDocumentKind
    format: Literal["pdf", "docx"]


class ExportJobResponse(BaseModel):
    """One `ExportJob` as the client sees it — the body of the `POST` (202 or 200), of
    `GET /api/export-jobs/{id}`, and of every row of the listing.

    One shape for all three, so the client has one parser and one renderer. The `POST` answers it
    with `status: "queued"` and most of the rest `null`; the poll answers the same shape filled in.

    **This body carries no PII and no path** (ADR-0016 (e)). There is no `file_key`, no filesystem
    path and not one character of the document — the row itself holds the document only by
    reference, and this response holds even less. The bytes live behind `file_url`, which is a
    second authorized request.

    Three deliberate choices, each carried at the field that embodies it — see `retryable`,
    `current` and `file_url` below.
    """

    id: UUID
    tailoring_run_id: UUID

    document: TailoredDocumentKind
    format: ExportFormat

    status: ExportJobStatus
    failure_reason: ExportFailureReason | None

    # **Computed at the boundary from `failure_reason`, never stored and never re-derived by the
    # client** (AC-24, Constitution §4.5). "Which failures are worth asking again for" is a business
    # rule, and the API is its only authority: `render_failed`, `output_too_large` and
    # `source_unavailable` are `False` — the same input would fail the same way, so offering *Export
    # again* for them would be selling the same refusal twice — and the other six are `True`. The
    # handler computes it with a `match` over `ExportFailureReason` closed by `assert_never` (I18),
    # which makes a reason added later a type error here rather than a silently-missing button in
    # the browser. A TypeScript copy of that rule would be a second authority that drifts.
    #
    # `False` for a job that has not failed, which is not a claim that it is unretryable — a
    # `queued`, `rendering` or `ready` job has nothing to retry, and the client only reads this
    # field when `status == "failed"`.
    retryable: bool

    # The run version this job was requested at, and whether that is still the run's version.
    #
    # **`current` is a cross-aggregate comparison** (`job.was_requested_for(run.version)`) and
    # neither aggregate can make it alone, which is why `GetExportJobForSession` hands back
    # `ExportJobLookup(job, run_version_now)` and `ListExportsForRun` hands back one `run_version`
    # for the whole listing. `false` when the run has moved on (the user edited a document after
    # clicking Export) **and** when the run is gone entirely: a job whose source no longer exists is
    # certainly not current. The UI reads it to choose between *Download* and *Export again*; the
    # file itself is still served for a stale ready job (X-46), because it is the user's own.
    #
    # `run_version` rides alongside rather than being folded away, so a client can say *your
    # document changed since this file was made* without a second request.
    run_version: int
    current: bool

    byte_size: int | None
    render_duration_ms: int | None

    # **`null` until `ready`**, and a path rather than an absolute URL: the client is same-origin
    # and the typed client prepends nothing. `/api/export-jobs/{id}/file`. Serving it before the
    # bytes exist would be an invitation to a 409, and building it in TypeScript would put the URL
    # grammar in two places.
    file_url: str | None

    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    # The **guest session's** expiry, not a property of the job row — the session owns the 24-hour
    # promise (ADR-0006) and carrying it here puts that promise in the payload as well as in the UI
    # copy, exactly as `BaseCvResponse`, `JobPostingResponse` and `TailoringRunResponse` do.
    expires_at: datetime


class ExportJobListResponse(BaseModel):
    """Every `ExportJob` requested for one run, newest first.

    `items` is `[]` for a run nobody has exported — an empty list is an ordinary answer to
    `GET /api/tailoring-runs/{id}/exports`, never a 404. That is the state of every run the moment
    it succeeds, and it is what the export bar renders on first paint.

    A run has two documents and two queued formats, so four in flight is the realistic maximum and
    the reason this endpoint is a list rather than a lookup: a browser refresh loses every job id
    the page was holding, and one request keyed on the run — which *is* in the URL — reattaches to
    all of them.
    """

    items: list[ExportJobResponse] = Field(default_factory=list)
