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

import time
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.domain.retention.errors import OrphanScanAborted
from tailorcraft.domain.retention.storage import StorageBucket
from tailorcraft.domain.retention.value_objects import OrphanScanReport, PurgeReport
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.deps import (
    AdminDep,
    ClockDep,
    OverdueBacklogDep,
    PurgeFactoryDep,
    PurgeHeartbeatDep,
    PurgeLockDep,
    SessionDep,
    SummarizeStorageDep,
    SweepFactoryDep,
    require_admin,
)
from tailorcraft.infrastructure.api.routers._admin_access import RetentionAction
from tailorcraft.infrastructure.api.routers._admin_responses import (
    FIREWALL,
    NO_STORE,
    refusal,
    set_no_store,
)
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
    StorageBucketResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.retention.lock import PurgeLockOutcome
from tailorcraft.infrastructure.retention.log_events import (
    EVENT_ORPHANS_ABORTED,
    EVENT_ORPHANS_COMPLETED,
    EVENT_ORPHANS_FAILED,
    EVENT_PURGE_COMPLETED,
    EVENT_PURGE_FAILED,
    EVENT_PURGE_SKIPPED,
    log_purge_failures,
)

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
    set_no_store(response)
    with RetentionAction(actor, "storage_overview") as action:
        try:
            overview = await summarize()
        except DomainError as exc:
            # `FileStoreUnavailable`: the root could not be walked → 503 `storage_unavailable`.
            raise action.refuse(refusal(exc)) from None

        wire = AdminStorageResponse(
            scanned_at=overview.scanned_at,
            guest=_bucket(overview.guest),
            user=_bucket(overview.user),
            unreferenced=_bucket(overview.unreferenced),
            partial=_bucket(overview.partial),
            unrecognized=_bucket(overview.unrecognized),
        )
        await db.commit()
        action.log(
            "ok",
            guest_files=overview.guest.files,
            user_files=overview.user.files,
            unreferenced_files=overview.unreferenced.files,
            partial_files=overview.partial.files,
            unrecognized_files=overview.unrecognized.files,
        )
        return wire


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
    set_no_store(response)
    with RetentionAction(actor, "purge_preview") as action:
        # The CLI's dry run (AC-16): no lock, no heartbeat — no Redis on this path at all. One
        # backlog count, the same instant's answer for `overdue`.
        overdue = await backlog.count()
        report = await build_purge(limit, True)()
        await db.commit()
        action.log("ok", **_purge_counts(report), overdue=overdue)
        return _purge_response(report, overdue=overdue, lock=None)


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
    clock: ClockDep,
) -> AdminPurgeResponse:
    """One batch of at most `limit` expired sessions, oldest first (AC-17 … AC-20): `dry_run:
    false`, `overdue` after the batch, `lock` as acquired."""
    set_no_store(response)
    started_at = time.monotonic()
    with RetentionAction(actor, "purge_run") as action:
        async with lock.hold() as hold:
            if not hold.may_run:
                # The CLI's exit 3, as a 409: a run that did nothing is never a 200 (AC-18). The
                # lock adapter's own release is a no-op for a lock this request does not hold.
                log.info(EVENT_PURGE_SKIPPED, reason="lock_held")
                raise action.refuse(
                    _refused(
                        status.HTTP_409_CONFLICT,
                        "purge_in_progress",
                        "A purge is already running. This run deleted nothing.",
                    )
                )

            try:
                report = await build_purge(body.limit, False)()
                log_purge_failures(report)
                # A fresh count after the batch — the number a job that is not working cannot fake.
                overdue = await backlog.count()
                await db.commit()
            except Exception as exc:
                # The CLI's R-1/R-15 line: the type only, never the message. Whatever committed stays
                # committed; **no heartbeat** — a run that died did not finish (AC-20).
                await db.rollback()
                log.warning(
                    EVENT_PURGE_FAILED,
                    error_type=type(exc).__name__,
                    duration_ms=_elapsed_ms(started_at),
                )
                raise action.refuse(
                    _refused(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        "purge_failed",
                        "The purge failed. Sessions already deleted stay deleted; try again.",
                    )
                ) from None

            duration_ms = _elapsed_ms(started_at)
            # `record` never raises (AC-19): the irreversible work is done by now.
            await heartbeat.record(
                at=clock.now(),
                outcome="ok",
                sessions_deleted=report.sessions_deleted,
                files_unlinked=report.files_unlinked,
                duration_ms=duration_ms,
            )
            # The CLI's completed line, field for field (`_log_purge_line`).
            log.info(
                EVENT_PURGE_COMPLETED,
                sessions_deleted=report.sessions_deleted,
                sessions_skipped=report.sessions_skipped,
                sessions_failed=report.sessions_failed,
                files_unlinked=report.files_unlinked,
                files_failed=report.files_failed,
                examined=report.examined,
                overdue_after=overdue,
                dry_run=False,
                duration_ms=duration_ms,
            )
            acquired = hold.outcome is PurgeLockOutcome.ACQUIRED
            action.log("ok", **_purge_counts(report), overdue=overdue)
            return _purge_response(
                report, overdue=overdue, lock="acquired" if acquired else "unavailable"
            )


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
    set_no_store(response)
    with RetentionAction(actor, "sweep_preview") as action:
        report = await _sweep(build_sweep(limit, True), action, db)
        action.log("ok", **_sweep_counts(report))
        return _sweep_response(report)


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
    set_no_store(response)
    with RetentionAction(actor, "sweep_run") as action:
        report = await _sweep(build_sweep(body.limit, False), action, db)
        action.log("ok", **_sweep_counts(report))
        return _sweep_response(report)


# --- Helpers ------------------------------------------------------------------------------------


async def _sweep(
    sweep: ReclaimOrphanedFiles, action: RetentionAction, db: AsyncSession
) -> OrphanScanReport:
    """Run the sweep as the CLI's `_reclaim_orphans` does, with its three lines, translating its two
    refusals: a cross-check that could not run is 503 `orphan_check_unavailable` with nothing
    unlinked (fails closed); a volume that could not be walked is 503 `storage_unavailable`."""
    started_at = time.monotonic()
    try:
        report = await sweep()
        await db.commit()
    except OrphanScanAborted as exc:
        await db.rollback()
        log.warning(
            EVENT_ORPHANS_ABORTED,
            error_type=type(exc).__name__,
            duration_ms=_elapsed_ms(started_at),
        )
        raise action.refuse(
            _refused(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "orphan_check_unavailable",
                "The database cross-check could not run, so nothing was unlinked. Try again.",
            )
        ) from None
    except Exception as exc:
        log.warning(
            EVENT_ORPHANS_FAILED, error_type=type(exc).__name__, duration_ms=_elapsed_ms(started_at)
        )
        if isinstance(exc, DomainError):
            # `FileStoreUnavailable` → 503 `storage_unavailable`.
            raise action.refuse(refusal(exc)) from None
        raise

    log.info(
        EVENT_ORPHANS_COMPLETED,
        scanned=report.scanned,
        referenced=report.referenced,
        too_young=report.too_young,
        unrecognized=report.unrecognized,
        reclaimed=report.reclaimed,
        failed=report.failed,
        dry_run=report.dry_run,
        duration_ms=_elapsed_ms(started_at),
    )
    return report


def _refused(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code, detail={"code": code, "message": message}, headers=dict(NO_STORE)
    )


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _bucket(bucket: StorageBucket) -> StorageBucketResponse:
    return StorageBucketResponse(files=bucket.files, bytes=bucket.bytes)


def _purge_counts(report: PurgeReport) -> dict[str, int]:
    return {
        "examined": report.examined,
        "sessions_deleted": report.sessions_deleted,
        "sessions_skipped": report.sessions_skipped,
        "sessions_failed": report.sessions_failed,
        "files_unlinked": report.files_unlinked,
        "files_failed": report.files_failed,
    }


def _purge_response(
    report: PurgeReport, *, overdue: int, lock: Literal["acquired", "unavailable"] | None
) -> AdminPurgeResponse:
    return AdminPurgeResponse(
        **_purge_counts(report), overdue=overdue, dry_run=report.dry_run, lock=lock
    )


def _sweep_counts(report: OrphanScanReport) -> dict[str, int]:
    return {
        "scanned": report.scanned,
        "referenced": report.referenced,
        "too_young": report.too_young,
        "unrecognized": report.unrecognized,
        "reclaimed": report.reclaimed,
        "failed": report.failed,
    }


def _sweep_response(report: OrphanScanReport) -> AdminSweepResponse:
    return AdminSweepResponse(**_sweep_counts(report), dry_run=report.dry_run)
