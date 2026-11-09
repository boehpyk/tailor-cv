"""The guest `export` paths, refused (slice 5.2, plan §0.7, AC-16, F-26).

Until 5.2 these five routes let a guest download a run's documents and render PDF/DOCX exports. A
guest can no longer tailor, so a guest can no longer export: every route answers 403
`account_required` through **one router-level dependency** (`deps.refuse_guest_export`), pinned on
`router.dependencies`. It runs before path validation, the cookie or a body is read, so a malformed id
is the same 403. The handlers take no parameters for the same reason and are never reached.

**Why keep the paths at all:** a bundle cached for up to 24 hours still calls them, and a removed path
that still matches another route answers 405 (CLAUDE.md), which no client knows how to explain. The
account's export routes are `routers/me_tailoring_runs.py` and `routers/me_export_jobs.py`; their
bodies are `_export_handlers.py`.

`prefix="/api"` with full paths because the five hang off two resources (`/tailoring-runs/{run_id}/…`
and `/export-jobs/{export_job_id}…`). Registered last in `main.py`: no path here shadows a tailoring
route.
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, status

from tailorcraft.infrastructure.api.deps import refuse_guest_export
from tailorcraft.infrastructure.api.errors import account_required_exception
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

router = APIRouter(prefix="/api", tags=["export"], dependencies=[Depends(refuse_guest_export)])

_REFUSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "account_required — always, since slice 5.2: exporting needs an account.",
    },
}


def _unreachable() -> NoReturn:
    """The router's dependency has already answered; this is the same answer, should it ever not."""
    raise account_required_exception()


@router.get(
    "/tailoring-runs/{run_id}/documents/{kind}/download", response_model=None, responses=_REFUSED
)
async def download_document_inline() -> NoReturn:
    _unreachable()


@router.post("/tailoring-runs/{run_id}/exports", response_model=None, responses=_REFUSED)
async def request_export() -> NoReturn:
    _unreachable()


@router.get("/tailoring-runs/{run_id}/exports", response_model=None, responses=_REFUSED)
async def list_exports_for_run() -> NoReturn:
    _unreachable()


@router.get("/export-jobs/{export_job_id}", response_model=None, responses=_REFUSED)
async def get_export_job() -> NoReturn:
    _unreachable()


@router.get("/export-jobs/{export_job_id}/file", response_model=None, responses=_REFUSED)
async def download_export_file() -> NoReturn:
    _unreachable()
