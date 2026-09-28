"""A signed-in user's tailoring runs — the history collection — and everything that hangs off one
(slice 2.3, technical plan §4, ADR-0023, ADR-0024).

Built red-first (sdlc.md §2): **SKELETON** (T20, this file — real paths, real schemas, every
documented status in each `responses=` map, handlers raising `NotImplementedError`), **RED** (T21,
`qa`), **GREEN** (T23 — thin calls into `_tailoring_handlers.py` and `_export_handlers.py`, T19's
shared bodies; the history list and `DELETE` are new).

**`/api/me/tailoring-runs` is the history** (plan §4): the resource *is* the user's runs; "history"
is the page that lists them. One collection, one noun. Its list is a different shape from the guest
list (`HistoryPageResponse`, keyset-paged, with the posting and the CV a user needs to recognise an
entry) because the guest list is 1.3's, for a 24-hour workspace.

**One credential: the bearer** (`require_user`); a `tc_guest` cookie is ignored and nothing here reads
it. There is no transfer route: an account run's inputs are all account data (plan §0.1(a)), so the
AST scan's exception set stays `{POST /api/base-cvs/copies}` (AC-25). A guest-owned id is a 404 here,
byte-identical to one that does not exist, as a user-owned id is on every guest route.

**Why this router also carries the run's download and export routes**, where the guest surface
splits them into `routers/export.py`: the prefix is the resource's owner. Everything under
`/api/me/tailoring-runs/{id}/…` answers to the same bearer and the same run, so it is one router;
the job resource, once it exists, is `routers/me_export_jobs.py`, as the guest one is.

Every response carries `Cache-Control: no-store`, and every `expires_at` is `null` (OQ-8).
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Query, Request, Response, status

from tailorcraft.domain.tailoring.history import HistoryPageSize
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.api.deps import RequireUserDep

# T23's history handler calls both. Imported at the skeleton because every `routers/*.py` module must
# be loaded when the app is: the R-10 scan in `test_auth_route_dependency_boundary.py` reads each one
# from `sys.modules`, and a router module nothing imports is a `KeyError` there.
from tailorcraft.infrastructure.api.routers._history_cursor import (  # noqa: F401 -- see the comment above
    decode_cursor,
    encode_cursor,
)
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    RATE_LIMITED,
    REQUEST_TOO_LARGE,
    SERVICE_UNAVAILABLE,
    TAILORING_RUN_NOT_FOUND,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.export import (
    CreateExportRequest,
    ExportJobListResponse,
    ExportJobResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.tailoring import (
    CreateTailoringRunRequest,
    HistoryPageResponse,
    ReviseDocumentRequest,
    TailoringRunResponse,
)

router = APIRouter(prefix="/api/me/tailoring-runs", tags=["tailoring"])

# A cursor is `base64url("<epoch>.<uuid>")` — about 70 characters. Anything far longer is not one of
# ours, and FastAPI refuses it as `validation_error` before the codec is asked (plan §0.5).
_CURSOR_MAX_LENGTH = 128


@router.post(
    "",
    response_model=TailoringRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": (
                "Accepted for processing: the run is committed `queued` and handed to a worker. "
                "Poll the `Location` URL. `expires_at: null`."
            ),
            "headers": {
                "Location": {
                    "description": "`/api/me/tailoring-runs/{id}` — the run to poll.",
                    "schema": {"type": "string"},
                },
            },
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": (
                "base_cv_not_found | job_posting_not_found — also for another user's or a guest's "
                "object, byte-identical to a nonexistent id."
            ),
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "base_cv_not_extracted | tailoring_already_running (+ `active_tailoring_run_id`) "
                "| too_many_tailoring_runs"
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "rate_limit_unavailable (the tailoring limiter fails closed — this endpoint "
                "spends money) | queue_unavailable (the row is committed and recorded "
                "`failed`/`not_queued` first) | service_unavailable"
            ),
        },
        **REQUEST_TOO_LARGE,
        **VALIDATION_ERROR,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
    },
)
async def request_my_tailoring_run(
    request: Request,
    response: Response,
    body: Annotated[CreateTailoringRunRequest, Body()],
    user_id: RequireUserDep,
) -> TailoringRunResponse:
    """Request one tailoring run from the account's own saved CV and posting — 1.3's contract, the
    principal `("user", <id>)` plus the client IP on the fail-closed budget."""
    raise NotImplementedError


@router.get(
    "",
    response_model=HistoryPageResponse,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "validation_error — `limit` outside 1…50 (H-27) | invalid_cursor — a malformed, "
                "tampered or wrong-shape cursor (H-26); the cursor is never echoed or logged."
            ),
        },
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def list_my_tailoring_history(
    response: Response,
    user_id: RequireUserDep,
    limit: Annotated[
        int, Query(ge=HistoryPageSize.MINIMUM, le=HistoryPageSize.MAXIMUM)
    ] = HistoryPageSize.DEFAULT,
    cursor: Annotated[str | None, Query(max_length=_CURSOR_MAX_LENGTH)] = None,
) -> HistoryPageResponse:
    """One keyset page of the account's history, newest first (ADR-0024). `next_cursor` is `null`
    on the last page; an empty history is `{"items": [], "next_cursor": null}`. No document body
    is ever in it (AC-55)."""
    raise NotImplementedError


@router.get(
    "/{tailoring_run_id}",
    response_model=TailoringRunResponse,
    responses={
        **TAILORING_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def get_my_tailoring_run(
    tailoring_run_id: UUID,
    response: Response,
    user_id: RequireUserDep,
) -> TailoringRunResponse:
    """One run in full with its **current** documents — 1.4's shape, `expires_at: null`."""
    raise NotImplementedError


@router.put(
    "/{tailoring_run_id}/documents/{kind}",
    response_model=TailoringRunResponse,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "document_version_conflict (+ `current_version`) | tailoring_run_not_editable "
                "(+ `status`) — 1.4's codes."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "validation_error | document_invalid (+ `problem`) — 1.4's codes.",
        },
        **TAILORING_RUN_NOT_FOUND,
        **REQUEST_TOO_LARGE,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def revise_my_tailored_document(
    tailoring_run_id: UUID,
    kind: TailoredDocumentKind,
    response: Response,
    body: Annotated[ReviseDocumentRequest, Body()],
    user_id: RequireUserDep,
) -> TailoringRunResponse:
    """Replace one of a `succeeded` run's current documents — 1.4's contract; the save budget is
    keyed on the principal `("user", <id>)` and fails open."""
    raise NotImplementedError


@router.delete(
    "/{tailoring_run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": (
                "The entry is gone: its run, its export jobs and — if no other run uses it — its "
                "posting, committed, then the export files unlinked (plan §0.6). A failed unlink "
                "is still a 204: the rows are what the user sees."
            ),
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_in_progress (+ `status`) — the run is `queued` or `running`; "
                "nothing was deleted (H-42). There is no cancellation: a paid call in flight "
                "completes."
            ),
        },
        **TAILORING_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def delete_my_history_entry(
    tailoring_run_id: UUID,
    response: Response,
    user_id: RequireUserDep,
) -> None:
    """Delete one history entry (`EraseHistoryEntry`). A second delete of the same id is a 404."""
    raise NotImplementedError


@router.get(
    "/{tailoring_run_id}/documents/{kind}/download",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The current document's bytes, with 1.5's headers: `Content-Type` from the "
                "format, `Content-Disposition` from the domain's constant filename, "
                "`Cache-Control: no-store`, `X-Content-Type-Options: nosniff`."
            ),
            "content": {"text/markdown": {}, "text/plain": {}},
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "tailoring_run_not_exportable (+ `status`)",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "render_failed — 1.5's one deliberate 500.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "render_timed_out | service_unavailable",
        },
        **TAILORING_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
    },
)
async def download_my_document_inline(
    tailoring_run_id: UUID,
    kind: TailoredDocumentKind,
    format: Annotated[
        Literal["md", "txt"],
        Query(
            description=(
                "`md` or `txt` only. PDF and DOCX are prepared by a worker — "
                "`POST /api/me/tailoring-runs/{tailoring_run_id}/exports`."
            ),
        ),
    ],
    user_id: RequireUserDep,
) -> Response:
    """Render a current document to Markdown or plain text inside the request — 1.5's contract.
    Returns its own `Response` (no injected one: its headers would be dropped, see 1.5)."""
    raise NotImplementedError


@router.post(
    "/{tailoring_run_id}/exports",
    response_model=ExportJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {
            "model": ExportJobResponse,
            "description": (
                "The current job for this (run, document, format) already exists and is returned "
                "unchanged; `Location` points at it."
            ),
        },
        status.HTTP_202_ACCEPTED: {
            "description": "A new job, committed `queued` and handed to a worker.",
            "headers": {
                "Location": {
                    "description": "`/api/me/export-jobs/{id}` — the job to poll.",
                    "schema": {"type": "string"},
                },
            },
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_not_exportable (+ `status`) | too_many_export_jobs — the run "
                "already has `max_export_jobs_per_user_run` jobs (a user's cap is per run)."
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "queue_unavailable | service_unavailable. Never rate_limit_unavailable: the "
                "export limiter fails open, as 1.5's does."
            ),
        },
        **TAILORING_RUN_NOT_FOUND,
        **REQUEST_TOO_LARGE,
        **VALIDATION_ERROR,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
    },
)
async def request_my_export(
    tailoring_run_id: UUID,
    request: Request,
    response: Response,
    body: Annotated[CreateExportRequest, Body()],
    user_id: RequireUserDep,
) -> ExportJobResponse:
    """Ask for a PDF or DOCX of one current document — 1.5's contract (202 new / 200 existing), the
    principal `("user", <id>)` plus the client IP on the fail-open budget."""
    raise NotImplementedError


@router.get(
    "/{tailoring_run_id}/exports",
    response_model=ExportJobListResponse,
    responses={
        **TAILORING_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def list_my_exports_for_run(
    tailoring_run_id: UUID,
    response: Response,
    user_id: RequireUserDep,
) -> ExportJobListResponse:
    """Every export job of one of the account's runs, newest first; `items: []` for none."""
    raise NotImplementedError
