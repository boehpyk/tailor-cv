"""A signed-in user's export jobs: poll one, download its file (slice 2.3, technical plan §4).

Built red-first (sdlc.md §2): **SKELETON** (T20, this file — real paths, real schemas, every
documented status in each `responses=` map, handlers raising `NotImplementedError`), **RED** (T21,
`qa`), **GREEN** (T23 — thin calls into `_export_handlers.py`, T19's shared bodies).

A job, once it exists, is its own resource (1.5's reasoning): its poll and its download name the job,
not the run. Jobs are **created** under their run (`POST /api/me/tailoring-runs/{id}/exports`), whose
`Location` and every `file_url` point here.

**One credential: the bearer** (`require_user`); a `tc_guest` cookie is ignored and nothing here reads
it. A guest's job id is a 404, byte-identical to one that does not exist. Every response carries
`Cache-Control: no-store`, and every `expires_at` is `null` (OQ-8).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from tailorcraft.infrastructure.api.deps import RequireUserDep
from tailorcraft.infrastructure.api.routers._me_responses import (
    EXPORT_JOB_NOT_FOUND,
    NOT_SIGNED_IN,
    SERVICE_UNAVAILABLE,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.export import ExportJobResponse
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

router = APIRouter(prefix="/api/me/export-jobs", tags=["export"])


@router.get(
    "/{export_job_id}",
    response_model=ExportJobResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The job — the resource the client polls, so a `failed` job is a 200 too. "
                "`file_url` is `/api/me/export-jobs/{id}/file` once `ready`."
            ),
        },
        **EXPORT_JOB_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def get_my_export_job(
    export_job_id: UUID,
    response: Response,
    user_id: RequireUserDep,
) -> ExportJobResponse:
    """One of the account's export jobs — 1.5's poll."""
    raise NotImplementedError


@router.get(
    "/{export_job_id}/file",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The stored bytes with 1.5's headers (`Content-Disposition` from the domain's "
                "constant filename, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`)."
            ),
            "content": {
                "application/pdf": {},
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {},
            },
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "export_not_ready (+ `status`, + `failure_reason` when failed)",
        },
        status.HTTP_410_GONE: {
            "model": ErrorResponse,
            "description": "export_file_gone — the row says `ready` and the store has nothing.",
        },
        **EXPORT_JOB_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def download_my_export_file(
    export_job_id: UUID,
    user_id: RequireUserDep,
) -> Response:
    """The rendered file of one of the account's `ready` jobs — 1.5's download. Returns its own
    `Response` (no injected one: its headers would be dropped, see 1.5)."""
    raise NotImplementedError
