"""The `export` HTTP surface: download a document as it stands, or ask for one to be rendered.

Built in three passes (docs/sdlc.md §2): **SKELETON** (I16 — this file as you first see it: real
paths, real signatures, real `responses=` maps, `NotImplementedError` bodies), **RED** (I17, `qa` —
the API tests against those exact signatures, every one of them failing on its *assertion*),
**GREEN** (I18 — the fail-open limiter, commit-then-enqueue, the response shaping, the headers and
the `DomainError` -> status translation, until those tests pass without a single edit to them).

The contract — five paths, the status codes, the schemas and every row of the failure contract —
came from `docs/specs/export-multi-format-download/` rather than from FastAPI, which is why this
tier is red-first at all. Everything below the boundary in this slice (the domain, the use cases,
the rendering pipeline, the mapping, the repository, the adapters, the queue, the tasks) was
test-after on purpose: its shape was discovered against WeasyPrint, markdown-it, SQLAlchemy and
Celery. The router's was not.

**Two resource shapes, one router, and that is why `prefix="/api"` and the full paths are spelled
out** — unlike `routers/tailoring.py`, which can say `prefix="/api/tailoring-runs"` and mean it.
Three of these routes hang off a run (`/api/tailoring-runs/{run_id}/...`) and two off a job
(`/api/export-jobs/{export_job_id}/...`), because the two are different resources with different
owners:

* **The collection nests.** "The exports of this run" is the question a refreshed page asks, and a
  refresh has the run id in the URL and no job ids at all. 1.4's documents nested for the same
  reason.
* **A job, once it exists, is its own resource.** It carries its own `guest_session_id` column and
  its own authorization link, and a poll or a download should not need the run id to name it. A
  nested `/tailoring-runs/{run}/exports/{id}` would make this router check that the job belongs to
  the run *and* to the session — a second rule with no purpose, and a second way to get it wrong.
  OQ-8 records the alternative.

Splitting them into two routers was the other option and was rejected: they share the `responses=`
fragments, the authorization dependency and the `_to_response` shaping, and a file boundary between
`POST …/exports` and `GET /export-jobs/{id}` would put the two halves of one flow in two places.

**The path parameter is `run_id` here and `tailoring_run_id` in `routers/tailoring.py`**, which is a
deliberate contradiction rather than drift: this file names two kinds of id in the same breath, and
`run_id` beside `export_job_id` reads as the pair it is. The technical plan's API table spells it
this way. The URL a client sends is identical either way — a path parameter's name is ours.

**`require_guest_session` on all five routes, and no session is minted anywhere in this router**
(ADR-0014 §3, X-2, X-12, X-42). `routers/intake.py` and `routers/posting.py` both mint a fresh
`GuestSession` on `POST` for a missing cookie, because those two *are* first contact — a visitor's
first upload or first pasted posting has to work with no cookie at all. Nothing here is a first
contact: every one of these five routes names a run or a job that a session must already own, so a
request with no cookie cannot be a new visitor, it is a visitor whose 24 hours ran out. Minting
would hand them a working session that owns nothing and then a 404, which reads as "your documents
were deleted"; the 401 says what actually happened. `routers/tailoring.py` made the same call.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Body, Query, Request, Response, status

from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.api.deps import (
    ClockDep,
    DownloadExportFileDep,
    EventPublisherDep,
    ExportJobRepositoryDep,
    ExportQueueDep,
    ExportRateLimiterDep,
    GetExportJobDep,
    ListExportsForRunDep,
    RenderDocumentInlineDep,
    RequestExportDep,
    RequireGuestSessionDep,
    SessionDep,
    SettingsDep,
)
from tailorcraft.infrastructure.api.schemas.export import (
    CreateExportRequest,
    ExportJobListResponse,
    ExportJobResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["export"])


# Shared `responses=` fragments, so one failure mode is documented with one shape at every handler
# that can produce it. `dict[str, Any]` because that is FastAPI's own type for a `responses=` entry
# (it takes a `type[BaseModel]` under "model" and a `str` under "description" in the same dict) —
# the `Any` is FastAPI's API, not a shortcut. `routers/tailoring.py` carries the identical note.
_GUEST_SESSION_EXPIRED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": (
            "guest_session_expired — missing, unknown or expired `tc_guest` cookie (X-2, X-12, "
            "X-42, X-50). **No session is minted**: none of these five routes is a first contact."
        ),
    },
}

_TAILORING_RUN_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "tailoring_run_not_found — **identical** for an id that does not exist and one owned "
            "by a different session (X-3, X-13); never a 403, which would confirm the id is real."
        ),
    },
}

_EXPORT_JOB_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "export_job_not_found — **identical** for an id that does not exist and one owned by "
            "a different session (X-43). An export job id is both the polling handle and the "
            "download handle, so it is the id someone enumerates, and a 403 would tell them when "
            "they had guessed right."
        ),
    },
}

_PATH_VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "validation_error — the path id is not a UUID (X-1, X-41).",
    },
}


# ---------------------------------------------------------------------------------------------
# The inline pair — `md` and `txt`, rendered inside the request, leaving no row and no file.
# ---------------------------------------------------------------------------------------------


@router.get(
    "/tailoring-runs/{run_id}/documents/{kind}/download",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The document's bytes. `Content-Type` from `ExportFormat.media_type` "
                "(`text/markdown; charset=utf-8` or `text/plain; charset=utf-8`), "
                '`Content-Disposition: attachment; filename="tailored-cv.md"` from the domain\'s '
                "`download_filename` — a **constant** keyed on (document, format), never the "
                "user's text (AC-27) — plus `Content-Length`, `Cache-Control: no-store` and "
                "`X-Content-Type-Options: nosniff`. The **current** document is served: the "
                "visitor's revision where one exists, else the model's draft (X-8). Nothing is "
                "written — no row, no file, no Redis key (AC-8)."
            ),
            "content": {"text/markdown": {}, "text/plain": {}},
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_not_exportable (X-4) — the run is `queued`, `running` or `failed`, "
                "so it has no current documents; the body carries `status`."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "validation_error — the run id is not a UUID, `kind` is not `cv` | "
                "`cover_letter`, or `format` is missing, unknown, or a **queued** format "
                "(`pdf` | `docx`) asked for here (X-1, AC-10). The query parameter's type is the "
                "inline subset, so a queued format is unrepresentable on this endpoint rather "
                "than rejected by a handler."
            ),
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": (
                "render_failed (X-5) — **the one deliberate 500 in this codebase.** A "
                "value-object-valid string the pipeline cannot render is our bug: nothing about "
                "the request was wrong, so it is not a 4xx, and a retry will not help, so a 503 "
                "would lie. It goes to Sentry with no locals. Do not 'fix' this row into a 503."
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "render_timed_out (X-6 — the render exceeded `export_inline_timeout_seconds`, 5) "
                "| service_unavailable (X-7 — Postgres down on the read)."
            ),
        },
        **_TAILORING_RUN_NOT_FOUND,
        **_GUEST_SESSION_EXPIRED,
    },
)
async def download_document_inline(
    run_id: UUID,
    kind: TailoredDocumentKind,
    format: Annotated[
        Literal["md", "txt"],
        Query(
            description=(
                "`md` or `txt` only. PDF and DOCX are prepared by a worker — "
                "`POST /api/tailoring-runs/{run_id}/exports`."
            ),
        ),
    ],
    session: RequireGuestSessionDep,
    render_inline: RenderDocumentInlineDep,
) -> Response:
    """Render one of a `succeeded` run's two documents to Markdown or plain text, inside the
    request, and answer the bytes as an attachment.

    **`Literal["md", "txt"]`, not `ExportFormat`** (AC-10, X-1). The inline/queued split is a
    property of the *endpoint*, and typing the query parameter as the inline subset makes a queued
    format unrepresentable here rather than rejected in a handler that could be edited to forget.
    `RenderDocumentInline` raises `ExportFormatNotInline` for the same rule (X-1's second lock),
    which is the one a future non-HTTP caller cannot route around. The third lock is that nothing
    stores an inline render: `FileRef.for_export` refuses `md` and `txt` outright.

    **There is no `response: Response` parameter here, and that is load-bearing rather than an
    omission.** This handler returns a `Response` it builds itself, and FastAPI merges the injected
    sub-response's headers into the reply *only* on the branch where the endpoint returns a model.
    Taking a `Response` parameter and setting `Cache-Control` on it would compile, read exactly like
    `routers/tailoring.py`'s reads, and silently send no such header — the `no-store` on a body that
    is a stranger's employment history. The headers go on the returned `Response`, all of them, in
    one place. The same applies to `download_export_file` below.

    **The render is CPU-bound and synchronous underneath** (markdown-it, then a string walk), so it
    runs in a thread under `asyncio.wait_for(export_inline_timeout_seconds)` — inside the adapter,
    which is where the timeout and the translation both live. A synchronous parse on the event loop
    would look fine with one user and stall every concurrent one at twenty (CLAUDE.md's 374 ms DOCX
    sniff, measured); AC-11 is the measurement that keeps this honest.

    SKELETON (I16): `qa` writes the tests, I18 writes the body.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------------------------
# The queued pair — `pdf` and `docx`, rendered by a worker, addressed as jobs.
# ---------------------------------------------------------------------------------------------


@router.post(
    "/tailoring-runs/{run_id}/exports",
    response_model=ExportJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {
            "model": ExportJobResponse,
            "description": (
                "**The current job for this (run, document, format) already exists** and is "
                "returned unchanged — `queued`, `rendering` or `ready`, at the run's current "
                "version (X-16, AC-13). No row was created and no task was published. `Location` "
                "is set to that job. **Not a 409**: a second render of the same version is not a "
                "conflict the user must resolve, it is the same resource — so the status code is "
                "the honest record of whether a row was created, and the body is the same shape "
                "either way, leaving the client one code path."
            ),
            "headers": {
                "Location": {
                    "description": "`/api/export-jobs/{id}` — the existing job to poll.",
                    "schema": {"type": "string"},
                },
            },
        },
        status.HTTP_202_ACCEPTED: {
            "description": (
                "Accepted for processing. The job is committed as `queued` and handed to a "
                "worker; `file_url` and `byte_size` are `null` until it finishes (AC-12). Poll "
                "the `Location` URL. The row is committed **before** the task is published "
                "(ADR-0014 §5), so a broker that refuses leaves a job the client can see rather "
                "than a task pointing at nothing."
            ),
            "headers": {
                "Location": {
                    "description": "`/api/export-jobs/{id}` — the new job to poll.",
                    "schema": {"type": "string"},
                },
            },
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_not_exportable (X-14 — the run is not `succeeded`; the body "
                "carries `status`) | too_many_export_jobs (X-18 — the session already owns "
                "`max_export_jobs_per_session` jobs; the body carries nothing, because the limit "
                "is not the client's business to display as a number)."
            ),
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorResponse,
            "description": (
                "request_too_large — the 256 KiB JSON body cap, refused on Content-Length before "
                "the body is parsed (X-11, `MaxBodySizeMiddleware`, unchanged since 1.2)."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "validation_error — the body is not JSON, a field is missing, `document` is "
                "unknown, `format` is an **inline** format (`md` | `txt`), an extra field is "
                'present (`extra="forbid"`), or the run id is not a UUID (X-10, AC-15). '
                "Also the use case's own second lock, `ExportFormatNotQueued` (X-15)."
            ),
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": (
                "rate_limited (X-19) — 30/hour/session and 60/hour/client-IP on `export:create`; "
                "carries a Retry-After header."
            ),
            "headers": {
                "Retry-After": {
                    "description": "Seconds until the smaller of the two budgets refills.",
                    "schema": {"type": "integer"},
                },
            },
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "queue_unavailable (X-22 — the broker refused the publish; the job is committed "
                "and re-recorded `failed`/`not_queued` in a second transaction before this "
                "answer) | service_unavailable (X-21 — Postgres down, or the commit failed; "
                "nothing survives the rollback and nothing was enqueued). **Never "
                "rate_limit_unavailable**: this limiter fails open (X-20), because a render costs "
                "worker seconds of ours and no money."
            ),
        },
        **_TAILORING_RUN_NOT_FOUND,
        **_GUEST_SESSION_EXPIRED,
    },
)
async def request_export(
    run_id: UUID,
    request: Request,
    response: Response,
    body: Annotated[CreateExportRequest, Body()],
    session: RequireGuestSessionDep,
    settings: SettingsDep,
    rate_limiter: ExportRateLimiterDep,
    request_export_job: RequestExportDep,
    jobs: ExportJobRepositoryDep,
    queue: ExportQueueDep,
    events: EventPublisherDep,
    clock: ClockDep,
    db: SessionDep,
) -> ExportJobResponse:
    """Ask for one document of one run to be rendered into one queued format, or get back the job
    already doing exactly that.

    **202 on a create, 200 on a return** (X-16, AC-13), and the declared status code is the 202: the
    200 branch sets `response.status_code` itself, which FastAPI honours because this handler
    returns a *model* rather than a `Response`. `RequestExportResult.created` is the flag that
    decides, and it exists precisely because the router cannot re-derive it — a `queued` job looks
    identical whether this request made it or found it.

    The order of the steps I18 writes is the contract, and each one's position is load-bearing —
    `routers/tailoring.py::request_tailoring_run` is the shape being copied:

    1. **The body cap** — `MaxBodySizeMiddleware`, before a byte is read (X-11 -> 413).
    2. **The shape** — `CreateExportRequest`, `extra="forbid"`, `format` the queued literal
       (X-10 -> 422), and the path's `run_id: UUID` (X-1 -> 422). Both before anything is spent.
    3. **The rate limiter** — `export:create`, both scopes, **fail-open** (X-19 -> 429 with
       `Retry-After`; X-20 -> the request proceeds and one log line records that Redis was
       unreachable, with the namespace and never the identifier). After shape validation, so a
       malformed request never consumes a counter.
    4. **`RequestExport`**, which authorizes the run through `GetTailoringRunForSession` (X-13),
       refuses a run that is not `succeeded` (X-14), enforces the per-session cap (X-18) and does
       the idempotent lookup (X-16).
    5. **The explicit commit**, then **the enqueue** — never the other way round (ADR-0014 §5). A
       task published before its row is committed can be picked up by a worker that finds nothing.
    6. **X-22's second transaction** on `ExportNotQueued`: the row is already committed, so there is
       nothing to roll back into "no job"; what is left is to tell the truth about the row that
       exists, `failed` / `not_queued`, so the client is not left polling a job that can never run.
       Copy `_record_not_queued`'s shape, including its "if this write fails too" note.

    SKELETON (I16): `qa` writes the tests, I18 writes the body.
    """
    raise NotImplementedError


@router.get(
    "/tailoring-runs/{run_id}/exports",
    response_model=ExportJobListResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Every job of this run, newest first, `Cache-Control: no-store`. `items` is `[]` "
                "for a run nobody has exported — **never a 404** (AC-24's list half). A run that "
                "is not `succeeded` is not an error here either: listing the exports of a run "
                "that has none is a good question whose answer is `[]`. The export bar is what "
                "checks the run's status; the listing does not."
            ),
        },
        **_TAILORING_RUN_NOT_FOUND,
        **_PATH_VALIDATION_ERROR,
        **_GUEST_SESSION_EXPIRED,
    },
)
async def list_exports_for_run(
    run_id: UUID,
    response: Response,
    session: RequireGuestSessionDep,
    list_exports: ListExportsForRunDep,
) -> ExportJobListResponse:
    """Every `ExportJob` requested for one run the caller owns.

    **This endpoint exists because a browser refresh loses every job id the page was holding.** One
    request keyed on the run — which *is* in the URL — reattaches the workspace to all of its jobs,
    instead of four polls against ids it no longer has. Two documents times two queued formats is
    four in flight at most, which is why this is the client's poller rather than four per-job ones.

    `ExportListing` hands back one `run_version` for the whole listing, and every row's `current` is
    judged against that same instant's fact — the comparison lives in one place.

    SKELETON (I16): `qa` writes the tests, I18 writes the body.
    """
    raise NotImplementedError


@router.get(
    "/export-jobs/{export_job_id}",
    response_model=ExportJobResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The job, `Cache-Control: no-store`. **This is the resource the client polls**, so "
                "it answers 200 for a `failed` job too: a job that reached a worker and failed is "
                "a recorded state of the resource, never a 5xx (AC-19). `retryable` and `current` "
                "are computed here (AC-24)."
            ),
        },
        **_EXPORT_JOB_NOT_FOUND,
        **_PATH_VALIDATION_ERROR,
        **_GUEST_SESSION_EXPIRED,
    },
)
async def get_export_job(
    export_job_id: UUID,
    response: Response,
    session: RequireGuestSessionDep,
    get_job: GetExportJobDep,
) -> ExportJobResponse:
    """One `ExportJob`, authorized by the link to the caller's guest session.

    The only 4xx on the happy polling path is "that job is not yours or does not exist", and the two
    are the same 404 because `GetExportJobForSession` raises the same type for both (X-43). Every
    outcome of a render — including every way it can fail — is a **200** with a `status` and a
    `failure_reason` (AC-19). A 5xx here would make a poller retry a decision that is already final.

    `export_job_id` is typed `UUID` so a malformed id is FastAPI's own 422 rather than something
    this handler rejects by hand (X-41).

    SKELETON (I16): `qa` writes the tests, I18 writes the body.
    """
    raise NotImplementedError


@router.get(
    "/export-jobs/{export_job_id}/file",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The stored bytes. `Content-Type: application/pdf` or "
                "`application/vnd.openxmlformats-officedocument.wordprocessingml.document`; "
                "`Content-Length` equal to the row's `byte_size`; "
                '`Content-Disposition: attachment; filename="tailored-cv.pdf"` from the domain\'s '
                "`download_filename`; `Cache-Control: no-store`; `X-Content-Type-Options: "
                "nosniff` (AC-25). A **stale** ready job is served (X-46) — the file is the user's "
                "own and refusing it buys nothing; the UI is what declines to offer it."
            ),
            "content": {
                "application/pdf": {},
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {},
            },
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "export_not_ready — the job is `queued` or `rendering` (X-44) or `failed` (X-45). "
                "The body carries `status`, and `failure_reason` when it failed, so the client "
                "renders the failure copy without a second request."
            ),
        },
        status.HTTP_410_GONE: {
            "model": ErrorResponse,
            "description": (
                "export_file_gone (X-47) — the row says `ready` and the store has nothing under "
                "its key: deleted by hand, the volume lost, a retention sweep mis-fired. 410 and "
                "not 404, because the job resource itself is right there; it is the file that is "
                "gone, and the answer is *Export again*."
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "service_unavailable (X-48) — the store is unreadable: EIO, permissions, the "
                "volume unmounted. `StoredFileMissing` is a **subclass** of `FileStoreUnavailable`, "
                "so the 410 branch must be caught first; this row is everything else."
            ),
        },
        **_EXPORT_JOB_NOT_FOUND,
        **_PATH_VALIDATION_ERROR,
        **_GUEST_SESSION_EXPIRED,
    },
)
async def download_export_file(
    export_job_id: UUID,
    session: RequireGuestSessionDep,
    download: DownloadExportFileDep,
) -> Response:
    """The rendered file of a `ready` job, as an attachment.

    Authorization is inherited whole from the poll — `DownloadExportFile` composes
    `GetExportJobForSession`, so "not mine" and "does not exist" are the same 404 here for the same
    reason (X-43). That composition is the point: an ownership check written a second time beside a
    file read is the one place in this slice where forgetting it hands a stranger a stranger's CV.

    **The read goes through `FileStorePort.get`**, which does its `open()` in a thread — never a
    synchronous read on the event loop. A 20 MiB PDF read inline would stall every concurrent user
    for as long as the disk takes, with no error and nothing logged.

    No `response: Response` parameter, for the reason `download_document_inline` documents in full:
    a handler that returns a `Response` never gets the injected sub-response's headers merged in,
    and `Cache-Control: no-store` silently vanishing off a body that *is* a person's CV is not a bug
    any test would catch by accident.

    SKELETON (I16): `qa` writes the tests, I18 writes the body.
    """
    raise NotImplementedError
