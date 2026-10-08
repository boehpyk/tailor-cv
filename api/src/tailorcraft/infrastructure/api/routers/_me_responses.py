"""`responses=` fragments shared by the three slice-2.3 account routers (`me_job_postings.py`,
`me_tailoring_runs.py`, `me_export_jobs.py`), so one failure mode is documented with one shape at
every account route that can produce it (technical plan §4).

`dict[int | str, dict[str, Any]]` is FastAPI's own type for a `responses=` entry (it takes a
`type[BaseModel]` under "model" and a `str` under "description" in the same dict) — the `Any` is
FastAPI's API, not a shortcut. `routers/saved_base_cvs.py` carries its own copies from 2.2.
"""

from __future__ import annotations

from typing import Any

from fastapi import status

from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

NOT_SIGNED_IN: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": (
            'invalid_access_token (+ WWW-Authenticate: Bearer error="invalid_token") — missing, '
            "expired or forged bearer | not_signed_in — a valid bearer whose account is gone. "
            "**Bearer only**: a `__Host-tc_guest` cookie is ignored on every `/api/me/` route."
        ),
    },
}

SERVICE_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "service_unavailable — Postgres down, or the commit failed.",
    },
}

VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "validation_error — a path id is not a UUID, or a query or body is malformed.",
    },
}

REQUEST_TOO_LARGE: dict[int | str, dict[str, Any]] = {
    status.HTTP_413_CONTENT_TOO_LARGE: {
        "model": ErrorResponse,
        "description": (
            "request_too_large — the 256 KiB JSON body cap, refused on Content-Length before the "
            "body is parsed (`MaxBodySizeMiddleware`)."
        ),
    },
}

RATE_LIMITED: dict[int | str, dict[str, Any]] = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": ErrorResponse,
        "description": "rate_limited — carries a Retry-After header.",
        "headers": {
            "Retry-After": {
                "description": "Seconds until the budget refills.",
                "schema": {"type": "integer"},
            },
        },
    },
}

TAILORING_RUN_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "tailoring_run_not_found — **byte-identical** for an id that does not exist, another "
            "user's run and a guest-owned run; never a 403, which would confirm the id is real."
        ),
    },
}

EXPORT_JOB_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "export_job_not_found — **byte-identical** for an id that does not exist, another "
            "user's job and a guest-owned job."
        ),
    },
}
