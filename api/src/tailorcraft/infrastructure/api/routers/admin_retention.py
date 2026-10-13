"""Storage, the guest purge and the orphan sweep from `/admin` (slice 4.4, technical plan §0.4,
§0.5, §0.6, §3, §4; ADR-0035, ADR-0018 amendment (c)).

Built red-first (sdlc.md §2): **SKELETON** (T13), **RED** (T14, `qa`), **GREEN** (T15).

**The CLI's use cases, called, not copied** (AC-8): the purge and the sweep are built from the CLI
root's constructors (`deps.get_purge_factory`, `deps.get_sweep_factory`), with `dry_run` and the
limit as the only differences. **The entry point owns the operational concerns** (§0.4), as the CLI
and the task do: a preview takes no lock and writes no heartbeat; a run takes `RedisPurgeLock` (the
same key and TTL), answers **409 `purge_in_progress`** when another holder has it — never 200, a run
that did nothing is not a success — runs anyway when Redis cannot answer (fail open, reported as
`lock: "unavailable"`), and writes the heartbeat only after a completed batch. The sweep **fails
closed**: a cross-check that could not run is 503 `orphan_check_unavailable` with nothing unlinked.

`require_admin` is router-level and every handler also takes `actor: AdminDep` (§0.6). Every
response a handler builds carries `Cache-Control: no-store`. No `Origin` check, no rate limit (the
purge serializes on its lock), no guest cookie (§0.8).
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, Response, status

from tailorcraft.infrastructure.api.deps import (
    AdminDep,
    OverdueBacklogDep,
    PurgeFactoryDep,
    PurgeHeartbeatDep,
    PurgeLockDep,
    SessionDep,
    SummarizeStorageDep,
    SweepFactoryDep,
    require_admin,
)
from tailorcraft.infrastructure.api.routers._admin_responses import FIREWALL
from tailorcraft.infrastructure.api.routers._me_responses import VALIDATION_ERROR
from tailorcraft.infrastructure.api.schemas.admin import (
    PURGE_LIMIT_DEFAULT,
    PURGE_LIMIT_MAX,
    SWEEP_LIMIT_MAX,
    AdminPurgeRequest,
    AdminPurgeResponse,
    AdminStorageResponse,
    AdminSweepRequest,
    AdminSweepResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

# `dict[str, Any]` is FastAPI's own type for a `responses=` entry; the `Any` is its API.
_SWEEP_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": (
            "orphan_check_unavailable — the database cross-check could not run, so nothing was "
            "unlinked (fails closed) | storage_unavailable — the volume could not be walked | "
            "service_unavailable"
        ),
    },
}


@router.get(
    "/storage",
    response_model=AdminStorageResponse,
    responses={
        **FIREWALL,
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "storage_unavailable — the volume could not be walked | "
            "service_unavailable",
        },
    },
)
async def get_storage(
    response: Response,
    actor: AdminDep,
    summarize: SummarizeStorageDep,
    db: SessionDep,
) -> AdminStorageResponse:
    """How the uploads volume splits: guest, user, unreferenced, partial, unrecognized (AC-15).
    Counts and bytes only — no name, key or path."""
    raise NotImplementedError


@router.get(
    "/guest-purge",
    response_model=AdminPurgeResponse,
    responses={**VALIDATION_ERROR, **FIREWALL},
)
async def preview_guest_purge(
    response: Response,
    actor: AdminDep,
    build_purge: PurgeFactoryDep,
    backlog: OverdueBacklogDep,
    db: SessionDep,
    limit: Annotated[int, Query(ge=1, le=PURGE_LIMIT_MAX)] = PURGE_LIMIT_DEFAULT,
) -> AdminPurgeResponse:
    """What one batch would take (AC-16): `dry_run: true`, `overdue` now, `lock: null`. No lock,
    no heartbeat, no row or file touched."""
    raise NotImplementedError


@router.post(
    "/guest-purge",
    response_model=AdminPurgeResponse,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "purge_in_progress — another run holds the lock; nothing was deleted and no "
                "heartbeat was written."
            ),
        },
        **VALIDATION_ERROR,
        **FIREWALL,
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "purge_failed — the batch raised; sessions already deleted stay deleted, no "
                "heartbeat | service_unavailable"
            ),
        },
    },
)
async def run_guest_purge(
    body: AdminPurgeRequest,
    response: Response,
    actor: AdminDep,
    build_purge: PurgeFactoryDep,
    backlog: OverdueBacklogDep,
    lock: PurgeLockDep,
    heartbeat: PurgeHeartbeatDep,
    db: SessionDep,
) -> AdminPurgeResponse:
    """One batch of at most `limit` expired sessions, oldest first (AC-17 … AC-20): `dry_run:
    false`, `overdue` after the batch, `lock` as acquired."""
    raise NotImplementedError


@router.get(
    "/orphan-sweep",
    response_model=AdminSweepResponse,
    responses={**VALIDATION_ERROR, **FIREWALL, **_SWEEP_UNAVAILABLE},
)
async def preview_orphan_sweep(
    response: Response,
    actor: AdminDep,
    build_sweep: SweepFactoryDep,
    db: SessionDep,
    limit: Annotated[int | None, Query(ge=1, le=SWEEP_LIMIT_MAX)] = None,
) -> AdminSweepResponse:
    """What the sweep would reclaim (AC-21): `dry_run: true`, nothing unlinked, no lock."""
    raise NotImplementedError


@router.post(
    "/orphan-sweep",
    response_model=AdminSweepResponse,
    responses={**VALIDATION_ERROR, **FIREWALL, **_SWEEP_UNAVAILABLE},
)
async def run_orphan_sweep(
    body: AdminSweepRequest,
    response: Response,
    actor: AdminDep,
    build_sweep: SweepFactoryDep,
    db: SessionDep,
) -> AdminSweepResponse:
    """Reclaim the unreferenced files past the floor (AC-21): `dry_run: false`, no lock."""
    raise NotImplementedError
