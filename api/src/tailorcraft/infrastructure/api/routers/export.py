"""The `export` HTTP surface: download a document as it stands, or ask for one to be rendered.

Built in three passes (docs/sdlc.md §2): **SKELETON** (I16 — this file as you first see it: real
paths, real signatures, real `responses=` maps, `NotImplementedError` bodies), **RED** (I17, `qa` —
the API tests against those exact signatures, every one of them failing on its *assertion*),
**GREEN** (I18 — the fail-open limiter, commit-then-enqueue, the response shaping, the headers and
the `DomainError` -> status translation, written until those tests passed without a single edit to
one of them).

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

from datetime import datetime
from typing import Annotated, Any, Literal, assert_never
from uuid import UUID

import structlog
from fastapi import APIRouter, Body, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.export.render_document_inline import RenderDocumentInlineCommand
from tailorcraft.application.export.request_export import RequestExportCommand
from tailorcraft.domain.export.errors import ExportNotQueued
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.ports import ExportJobRepository
from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
    download_filename,
)
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
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
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.schemas.export import (
    CreateExportRequest,
    ExportJobListResponse,
    ExportJobResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.rate_limit import RateLimitDecision, RateLimitScope, client_ip

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
# Boundary helpers. Pure functions over a loaded aggregate — no I/O, so they need no fixture to
# reason about — plus the one second-transaction coroutine X-22 needs.
# `routers/tailoring.py` carries the same three shapes one context over.
# ---------------------------------------------------------------------------------------------


def _is_retryable(reason: ExportFailureReason | None) -> bool:
    """Whether *Export again* is worth offering for a job that failed for `reason` (AC-24).

    **A business rule, and the API is its only authority** (Constitution §4.5): the React client
    branches on the boolean and never re-derives it. `render_failed`, `output_too_large` and
    `source_unavailable` are the three `False` answers because the same input renders to the same
    refusal — offering the button would be selling that refusal twice. The other six are transient
    (a timeout, a store that was briefly unreadable, a broker that was down, a swept worker, a
    document that has since changed, an unnamed residual) and a new job may well succeed.

    `None` — a job that has not failed — is `False`, which is not a claim that it is unretryable:
    a `queued`, `rendering` or `ready` job has nothing to retry, and the client reads this field
    only when `status == "failed"`.

    **A `match` closed by `assert_never`, not a set membership test**, for the reason
    `routers/tailoring.py::_is_retryable` gives at length: `reason in {…}` would compile today and
    silently answer `True` for a tenth reason added next month — which may well be one that must
    never be retried. Here that reason is a `mypy --strict` error naming the member, at the one line
    where somebody has to decide.
    """
    match reason:
        case None:
            return False
        case (
            ExportFailureReason.RENDER_FAILED
            | ExportFailureReason.OUTPUT_TOO_LARGE
            | ExportFailureReason.SOURCE_UNAVAILABLE
        ):
            return False
        case (
            ExportFailureReason.RENDER_TIMED_OUT
            | ExportFailureReason.FILE_STORE_UNAVAILABLE
            | ExportFailureReason.SOURCE_CHANGED
            | ExportFailureReason.NOT_QUEUED
            | ExportFailureReason.ABANDONED
            | ExportFailureReason.RENDER_ERROR
        ):
            return True
        case _:
            assert_never(reason)


def _to_response(
    job: ExportJob, *, run_version_now: int | None, expires_at: datetime
) -> ExportJobResponse:
    """One `ExportJob` as the wire sees it — the body of the `POST`, of the poll, and of every row
    of the listing, so the client has one parser and one renderer.

    Built by keyword, never `model_validate(job)`: three of the sixteen fields exist on no aggregate
    (`ExportJobResponse`'s docstring says so, which is why it sets no `from_attributes`).

    **`current` is the cross-aggregate comparison, and this is the one place it is made.** Neither
    aggregate can answer it alone, which is why `GetExportJobForSession` hands back
    `run_version_now` beside the job and `ListExportsForRun` hands back one `run_version` for the
    whole listing. `None` — the run is gone entirely — is `false`: a job whose source no longer
    exists is certainly not current. `was_requested_for` rather than `==` written out here, because
    the comparison is the aggregate's to define and this function only supplies the operand.

    **`file_url` is `null` until `ready`**, and it is a path: the client is same-origin. Serving it
    before the bytes exist would be an invitation to a 409, and building it in TypeScript would put
    the URL grammar in two places.

    `expires_at` is the **guest session's** (ADR-0006) — the session owns the 24-hour promise, and
    `BaseCvResponse`, `JobPostingResponse` and `TailoringRunResponse` all carry it the same way.
    """
    return ExportJobResponse(
        id=job.id.value,
        tailoring_run_id=job.tailoring_run_id.value,
        document=job.document,
        format=job.format,
        status=job.status,
        failure_reason=job.failure_reason,
        retryable=_is_retryable(job.failure_reason),
        run_version=job.run_version,
        current=run_version_now is not None and job.was_requested_for(run_version_now),
        byte_size=job.byte_size,
        render_duration_ms=job.render_duration_ms,
        file_url=(
            f"{router.prefix}/export-jobs/{job.id.value}/file"
            if job.status is ExportJobStatus.READY
            else None
        ),
        requested_at=job.requested_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        expires_at=expires_at,
    )


def _no_store(response: Response) -> None:
    """`Cache-Control: no-store` on the two reads that answer a *model*.

    The two binary routes do **not** use this: they build their own `Response` and set the header on
    it, because an injected sub-response's headers are merged only when the endpoint returns a model
    (`download_document_inline`'s docstring has the measurement). Sharing one helper across both
    shapes would be the trap, not the tidiness.

    An `ExportJobResponse` carries no document text, but it still enumerates what a named session is
    preparing and when — and an intermediary caching it would hand the next visitor on a shared
    address a list of somebody's job applications. 1.2 established the header; 1.4 carried it.
    """
    response.headers["Cache-Control"] = "no-store"


def _download_headers(document: TailoredDocumentKind, format: ExportFormat) -> dict[str, str]:
    """The three headers both binary routes set, in the order they are written here (AC-8, AC-25).

    **`no-store` and `nosniff` come first, before the filename** — literally, in this dict — because
    they are the two that must never be lost to an edit that reorders the ones around them. The body
    below them is a stranger's employment history: without `no-store` it can land in a shared cache,
    and without `nosniff` a browser may re-decide the type of a document it was handed as an
    attachment.

    **`Content-Disposition` comes from the domain's `download_filename`, which is a constant keyed
    on (document, format) and never touches the user's text** (AC-27). That is a rule rather than a
    formatting choice: a tailored CV's first line is whatever the model wrote from whatever the user
    uploaded, and a header assembled from it is a header an attacker gets to write —
    `'"; filename="evil.exe'` as the document's opening line is exactly the fixture AC-27 exports.
    Nothing here interpolates anything but that constant, so there is no injection to escape.

    `Content-Type` is not set here: it rides as the `Response`'s `media_type`, from
    `ExportFormat.media_type`, so Starlette owns the one header it also owns the charset rule for.
    `Content-Length` is Starlette's too, computed from the bytes it is handed rather than from a
    number we claim.
    """
    return {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{download_filename(document, format)}"',
    }


async def _record_not_queued(
    job_id: ExportJobId,
    jobs: ExportJobRepository,
    events: EventPublisherPort,
    clock: Clock,
    db: AsyncSession,
) -> None:
    """X-22's **second transaction**: record a committed job the broker refused as `failed` /
    `not_queued`, so the client is not left polling a job that can never run.

    `routers/tailoring.py::_record_not_queued`'s shape, copied deliberately rather than generalised
    into a shared helper over two aggregates — the two differ in the enum they write and in nothing
    else today, and CLAUDE.md's rule is that shared shape is not shared behaviour.

    A separate transaction from the one that created the row, and it has to be: that one is already
    committed — on purpose, *before* the enqueue (ADR-0014 §5) — so there is nothing left to roll
    back into "no job". What remains is to tell the truth about the row that exists. `mark_failed`
    is legal from `queued` for exactly this case (`ExportNotRendering` is deliberately not applied
    to it), and it leaves `started_at` `None`, because no worker ever began a render.

    **If this write fails too, the job stays `queued`, and that is the chosen survivor rather than a
    gap** — ADR-0006 §2's rule: of the crash windows available, pick the one whose survivor is
    recoverable. A `queued` job with no task is visible in the database, reads to the user as a
    render still waiting for a worker, and is either re-enqueued by hand or swept. Nothing is raised
    from here in that branch: the caller answers 503 `queue_unavailable` either way, which is the
    true answer to what the user asked for whichever of the two states the row ended in.

    The event is published only **after** the commit, so a log line never announces a `failed` state
    that a rollback is about to un-happen.
    """
    try:
        job = await jobs.get(job_id)
        job.mark_failed(ExportFailureReason.NOT_QUEUED, clock.now())
        await jobs.save(job)
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        # The job stays `queued` — the recoverable survivor. The id and the exception's type name
        # only: no message (a driver error can quote bound parameters) and no client address.
        log.warning(
            "export.not_queued_unrecorded",
            export_job_id=str(job_id.value),
            error_type=type(exc).__name__,
        )
        return
    except Exception as exc:
        # **THE FLOOR**, and it has a concrete scenario rather than a hypothetical one, the same one
        # `routers/tailoring.py` met: the publish the queue reported as refused actually *reached*
        # the broker (a timeout after the write), a worker picked the task up, and by the time
        # `mark_failed` runs here the job is already decided — so the aggregate refuses the
        # transition with a `DomainError`, which is not a `SQLAlchemyError`. Without this branch that
        # escapes as a bare 500 and breaks X-22's promise that every way this second write can fail
        # still answers 503 `queue_unavailable`. `ExportJobConcurrentlyModified` from `save` lands
        # here too, and so does a driver error outside SQLAlchemy's own tree.
        #
        # The fully-qualified TYPE, never `str(exc)` (a domain error is harmless, a driver's is not,
        # and this branch cannot tell which it holds) and never `exc_info`. Nothing is re-raised, so
        # there is no chain to cut. `Exception`, never `BaseException`: a cancelled request must
        # still cancel.
        await db.rollback()
        log.warning(
            "export.not_queued_unrecorded",
            export_job_id=str(job_id.value),
            error_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )
        return

    await events.publish(*job.release_events())


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

    **Nothing here is written, and nothing here is rate-limited.** An inline render is a parse and
    a string walk over text the caller already owns and can already read through the run resource —
    no row, no file, no Redis key, no money (AC-8). The `POST` below is the endpoint that buys
    worker seconds and disk, and it is the one that carries a limiter.
    """
    try:
        rendered = await render_inline(
            RenderDocumentInlineCommand(
                guest_session_id=session.id,
                tailoring_run_id=TailoringRunId(run_id),
                document=kind,
                # The query parameter is the inline literal; widening it back to the domain enum
                # happens **here, once** (the schema module's note), so nothing downstream has to
                # know that this endpoint's type is a subset. `RenderDocumentInline` re-checks
                # `format.delivery` anyway — that is the lock a non-HTTP caller cannot route around.
                format=ExportFormat(format),
            )
        )
    except DomainError as exc:
        # `TailoringRunNotExportable` -> 409 with `status` (X-4); `DocumentRenderTimedOut` -> 503
        # (X-6); every other `DocumentRenderFailed` -> the deliberate 500 (X-5). All of it in
        # `errors.py`, so the one mapping stays the one mapping.
        #
        # **`SQLAlchemyError` is deliberately not caught here** (X-7): it is not a `DomainError`, so
        # it passes straight through to the app-level handler that answers 503 `service_unavailable`
        # without this frame — whose locals would otherwise hold the document — in the chain.
        raise domain_error_to_http_exception(exc) from exc

    return Response(
        content=rendered.data,
        media_type=rendered.format.media_type,
        headers=_download_headers(rendered.document, rendered.format),
    )


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

    """
    # -- 1. The rate limiter, both scopes, FAIL-OPEN (X-19, X-20). --------------------------------
    #
    # **Open, where `request_tailoring_run`'s twin is closed**, and the difference is the invoice
    # (1.1's OQ-7 rule). A tailoring run spends money at a third party, so its limiter refuses the
    # request when it cannot count — an endpoint that spends money with its only backstop switched
    # off is a funded denial-of-wallet. An export spends worker seconds and disk of *ours*, both
    # already bounded by `max_export_jobs_per_session`, the 20 MiB output cap and the 24-hour purge.
    # With no invoice on the other side, refusing a legitimate export because Redis blinked costs
    # the visitor their download and costs an attacker nothing.
    #
    # So there is deliberately **no `RateLimiterUnavailable` branch here**, and its absence is the
    # decision rather than an omission: `deps.get_export_rate_limiter` constructs this limiter with
    # `fail_open=True`, so `check` returns `allowed=True` and logs one warning (scope, namespace and
    # the error type — never the identifier, which for the IP scope IS the client's address) instead
    # of raising. A `try/except` for the exception it cannot raise would be dead code that also reads
    # as though this endpoint might answer 503 `rate_limit_unavailable`, which it never does.
    #
    # Before the use case, so a 429 creates no row and spends nothing (ADR-0014 §2). After FastAPI's
    # own body validation, so a malformed request never consumes a counter. Both scopes are always
    # checked, never short-circuited on the first: a client over its session budget has still made
    # an attempt from its IP, and that attempt counts.
    checks: tuple[tuple[RateLimitScope, str, int], ...] = (
        ("session", str(session.id.value), settings.export_rate_limit_per_hour),
        (
            "ip",
            client_ip(request, settings.trusted_proxy_hops),
            settings.export_rate_limit_per_ip_per_hour,
        ),
    )
    decisions: list[tuple[RateLimitScope, RateLimitDecision]] = [
        (scope, await rate_limiter.check(scope, identifier, limit))
        for scope, identifier, limit in checks
    ]
    denied = [(scope, decision) for scope, decision in decisions if not decision.allowed]
    if denied:
        for scope, _decision in denied:
            # `scope` and `namespace` only. No identifier, and no job id — there is no job yet.
            log.info("rate_limit.exceeded", scope=scope, namespace=rate_limiter.namespace)
        retry_after = max(decision.retry_after_seconds for _scope, decision in denied)
        # **Seconds, and X-19's "Too many exports — try again in N minutes" is a different string.**
        # /verify flagged the two as disagreeing; the seconds won, and here is why rather than a
        # rewording. `message` is never shown to anyone: `exportCopy.ts` keys its sentences on
        # `code` on purpose ("`code` is the contract, `message` is prose for a human and can be
        # reworded without notice"), so the minutes in the spec's *User sees* column are the
        # frontend's copy to write, exactly as G-11's are in `apiErrorCopy.ts`. What this string
        # does have to agree with is `Retry-After`, which HTTP defines in seconds and which is the
        # only number a client can act on — and with the four sibling routers, which all say
        # seconds. Rounding it to minutes here would put a second unit on one fact.
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"Too many exports. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after)},
        )

    # -- 2. The use case (X-13, X-14, X-15, X-16, X-18). ------------------------------------------
    try:
        result = await request_export_job(
            RequestExportCommand(
                guest_session_id=session.id,
                tailoring_run_id=TailoringRunId(run_id),
                document=body.document,
                format=ExportFormat(body.format),
            )
        )
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    job = result.export_job
    # **Read the id into a local, and build the wire shape, BEFORE the commit** (1.4's lesson,
    # CLAUDE.md): a failed flush expires the whole identity map *inside* the flush, so every
    # attribute read after it is a lazy load, which on an `AsyncSession` is a `MissingGreenlet` —
    # and X-21's 503 would arrive as a 500 from the error path itself.
    job_id = job.id
    # `run_version_now=job.run_version`, and the tautology is the honest shape rather than a shortcut.
    # Both branches of `RequestExport` guarantee the equality: step 3 hands back an existing job only
    # if `was_requested_for(run.version)` held, and step 5 reads `run_version` off the run it just
    # authorized. `current` is therefore `true` on every 202 and every 200 this handler can send.
    # The alternative — a second read of the run here purely to compare it with itself — would buy a
    # primary-key lookup per export and could only ever disagree by racing the revision endpoint,
    # in which case the *poll* (which does compare against a fresh read) is the one telling the truth.
    wire = _to_response(job, run_version_now=job.run_version, expires_at=session.expires_at)

    # -- 3. Commit HERE, inside this handler's own error boundary (X-21). -------------------------
    #
    # Not left to `get_session`'s teardown: FastAPI runs the exit half of a yield-dependency AFTER
    # the response is sent, so a commit failing there would fire with the 202 already on the wire and
    # a task already published for a row that then never existed. X-21 is only reachable because the
    # commit is here.
    try:
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        # Nothing survives the rollback and — the point of commit-then-enqueue — nothing was
        # enqueued either, so no task anywhere names an id with no row. The type only, never the
        # message: a driver error quotes bound parameters.
        log.error("export.request_not_committed", error_type=type(exc).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "Something went wrong starting your export. Please try again.",
            },
        ) from exc

    # -- 4. THEN enqueue, and only for a job this request created (ADR-0014 §5, X-16, X-22). ------
    #
    # Commit-then-enqueue, never the other way round: the worker is a separate process picking tasks
    # up in milliseconds, so enqueue-then-commit is a race that fires under ordinary load — the task
    # reads an id whose row is not committed, returns MISSING, and the job sits `queued` for ever.
    # This order instead leaves a window whose survivor is a `queued` job with no task: visible, and
    # recoverable by re-enqueuing.
    #
    # `result.created` is the flag, and it exists because the router cannot re-derive it — a `queued`
    # job looks identical whether this request made it or found it. A second publish for a job
    # already in flight would be a second render and a second file for one click.
    if result.created:
        try:
            await queue.enqueue(job_id)
        except ExportNotQueued as exc:
            # X-22. The row is committed; record the truth about it in a second transaction, then
            # answer 503 whichever way that write went (`_record_not_queued` covers both branches).
            await _record_not_queued(job_id, jobs, events, clock, db)
            raise domain_error_to_http_exception(exc) from exc

    # -- 5. Tell the client where to poll, and which of the two answers this is. -------------------
    #
    # `Location` on **both** branches: the question "where do I poll for this?" has the same answer
    # whether the job was created now or found already running.
    #
    # The declared status code is the 202; the 200 branch sets `response.status_code` itself, which
    # FastAPI honours precisely because this handler returns a *model*. The two binary routes in
    # this file cannot do that and say so at length — see `download_document_inline`.
    response.headers["Location"] = f"{router.prefix}/export-jobs/{job_id.value}"
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return wire


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

    """
    _no_store(response)
    try:
        listing = await list_exports(TailoringRunId(run_id), session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    # One `run_version` for every row: the listing is a single instant's answer, and judging half
    # the rows against one fact and half against another read a moment later is how a client ends
    # up rendering *Download* beside *Your document changed*.
    return ExportJobListResponse(
        items=[
            _to_response(job, run_version_now=listing.run_version, expires_at=session.expires_at)
            for job in listing.jobs
        ]
    )


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

    """
    _no_store(response)
    try:
        lookup = await get_job(ExportJobId(export_job_id), session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    # A `failed` job is a **200** with `status` and `failure_reason` (AC-19), never a 5xx: the render
    # reached a worker and the worker recorded an outcome, which is a state of this resource. A 5xx
    # would make a poller retry a decision that is already final.
    return _to_response(
        lookup.job, run_version_now=lookup.run_version_now, expires_at=session.expires_at
    )


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

    """
    try:
        job, data = await download(ExportJobId(export_job_id), session.id)
    except DomainError as exc:
        # `ExportJobNotFound` -> 404 (X-43); `ExportNotReady` -> 409 carrying `status` and, when it
        # failed, `failure_reason` (X-44, X-45); `StoredFileMissing` -> **410** and
        # `FileStoreUnavailable` -> 503 `storage_unavailable` (X-47, X-48) — the narrow one pinned
        # above the wide one in `errors.py`, where the comment between them explains why the order
        # is the branch. A stale `ready` job is not an error at all and never reaches here (X-46):
        # the file is the user's own, and the UI is what declines to offer it.
        raise domain_error_to_http_exception(exc) from exc

    return Response(
        content=data,
        media_type=job.format.media_type,
        headers=_download_headers(job.document, job.format),
    )
