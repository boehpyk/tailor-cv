"""A signed-in user's application board: the board itself and the cards on it (slice 3.1, technical
plan §3 and §4, ADR-0029, ADR-0024 amendment (a)).

Built red-first (sdlc.md §2): **SKELETON** (T20 — real paths, real schemas, every documented status
in each `responses=` map, bodies `raise NotImplementedError`), **RED** (T21, `qa`), **GREEN** (T22 —
the limiter, the value-object construction, the domain-error translation, the in-handler commit).

**Two resources, one router.** `/api/me/board` is the read model (every card, rendered whole) and
`/api/me/tracked-applications` is the collection of cards a write touches. Both answer to the same
bearer and the same aggregate, so one module; the prefix is `/api/me` and each route spells its own
resource. Neither shares a prefix with another router, so its registration position is free.

**One credential: the bearer** (`require_user`); a `tc_guest` cookie is ignored and nothing here reads
it. **No transfer route** (AC-22): the AST scan's exception set stays exactly
`{POST /api/me/guest-work/claim}`.

**Every write** passes the tracking limiter first (`TrackingWriteRateLimiterDep`: `user` scope, one
600/h budget shared by the four writes, **fails open** — T-26, T-27). The board's `GET` is unlimited.

**The unit of work ends in the handler** (CLAUDE.md, the FastAPI 0.141 footgun: a dependency's
teardown runs after the response is sent), so a failed commit is a 503 and never a 2xx claiming a
write that did not land (T-28). Every response the handler builds carries `Cache-Control: no-store`.

**Logging**: ids only — never a title, a posting's title or URL, a CV label (Constitution §8).
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Response, status

from tailorcraft.infrastructure.api.deps import (
    MoveTrackedApplicationDep,
    RequireUserDep,
    RetitleTrackedApplicationDep,
    SessionDep,
    SettingsDep,
    ShowApplicationBoardDep,
    TrackApplicationDep,
    TrackingWriteRateLimiterDep,
    UntrackApplicationDep,
)
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    RATE_LIMITED,
    REQUEST_TOO_LARGE,
    SERVICE_UNAVAILABLE,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.tracking import (
    BoardResponse,
    MoveTrackedApplicationRequest,
    RetitleTrackedApplicationRequest,
    TrackApplicationRequest,
    TrackedApplicationResponse,
)

router = APIRouter(prefix="/api/me", tags=["tracking"])

# `dict[int | str, dict[str, Any]]` is FastAPI's own type for a `responses=` entry (see
# `_me_responses.py`); the `Any` is FastAPI's API, not a shortcut.
_TRACKED_APPLICATION_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "tracked_application_not_found — **byte-identical** for a card that does not exist, "
            "another user's card and one already untracked (T-22, T-23, T-31)."
        ),
    },
}

_VERSION_CONFLICT: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": (
            "tracked_application_version_conflict (+ `current_version`) — a stale `version` "
            "(T-20, the card's current number), or the card changed or vanished between the "
            "handler's read and its write (T-21, `current_version: null`)."
        ),
    },
}

_WRITE_VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "validation_error — a path id is not a UUID, an unknown key, an unknown stage, "
            "`version` below 1, or a title that is empty, over 120 characters or holds a control "
            "character (T-16). The title is never echoed."
        ),
    },
}

_PATH_VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "validation_error — the path id is not a UUID.",
    },
}


@router.get(
    "/board",
    response_model=BoardResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Every card of the user, `stage_changed_at DESC, id DESC`, unpaginated; "
                '`{"items": []}` when the board is empty (AC-30).'
            ),
        },
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def get_my_board(
    response: Response,
    user_id: RequireUserDep,
    show_board: ShowApplicationBoardDep,
    db: SessionDep,
) -> BoardResponse:
    """The whole board, one statement (ADR-0024 amendment (a)). No limiter."""
    raise NotImplementedError


@router.post(
    "/tracked-applications",
    response_model=TrackedApplicationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_201_CREATED: {
            "description": "The card, created in `stage` (default `to_apply`).",
            "headers": {
                "Location": {
                    "description": "`/api/me/tracked-applications/{id}` — the new card.",
                    "schema": {"type": "string"},
                },
            },
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_not_found — **byte-identical** for a run that does not exist, "
                "another user's run and a guest-owned run (T-13), and for a history entry deleted "
                "while the request was in flight (T-17)."
            ),
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_not_trackable — the run is queued, running or failed (T-12) | "
                "application_already_tracked (+ `tracked_application_id`, the existing card) "
                "(T-14, T-19) | too_many_tracked_applications — the per-user cap (T-15)."
            ),
        },
        **REQUEST_TOO_LARGE,
        **_WRITE_VALIDATION_ERROR,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def track_my_application(
    response: Response,
    body: Annotated[TrackApplicationRequest, Body()],
    user_id: RequireUserDep,
    settings: SettingsDep,
    limiter: TrackingWriteRateLimiterDep,
    track: TrackApplicationDep,
    db: SessionDep,
) -> TrackedApplicationResponse:
    """Put one of the user's succeeded runs on the board: **201** + `Location`."""
    raise NotImplementedError


@router.put(
    "/tracked-applications/{tracked_application_id}/stage",
    response_model=TrackedApplicationResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The card in its new stage with its new `version` — or unchanged, `version` "
                "included, when it already stood there (T-24)."
            ),
        },
        **_TRACKED_APPLICATION_NOT_FOUND,
        **_VERSION_CONFLICT,
        **REQUEST_TOO_LARGE,
        **_WRITE_VALIDATION_ERROR,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def move_my_tracked_application(
    tracked_application_id: UUID,
    response: Response,
    body: Annotated[MoveTrackedApplicationRequest, Body()],
    user_id: RequireUserDep,
    settings: SettingsDep,
    limiter: TrackingWriteRateLimiterDep,
    move: MoveTrackedApplicationDep,
    db: SessionDep,
) -> TrackedApplicationResponse:
    """Move a card to another stage, against the `version` the client last saw."""
    raise NotImplementedError


@router.put(
    "/tracked-applications/{tracked_application_id}/title",
    response_model=TrackedApplicationResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The card with its new title (`null` clears it) and new `version` — or unchanged "
                "when the title is the same (T-24)."
            ),
        },
        **_TRACKED_APPLICATION_NOT_FOUND,
        **_VERSION_CONFLICT,
        **REQUEST_TOO_LARGE,
        **_WRITE_VALIDATION_ERROR,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def retitle_my_tracked_application(
    tracked_application_id: UUID,
    response: Response,
    body: Annotated[RetitleTrackedApplicationRequest, Body()],
    user_id: RequireUserDep,
    settings: SettingsDep,
    limiter: TrackingWriteRateLimiterDep,
    retitle: RetitleTrackedApplicationDep,
    db: SessionDep,
) -> TrackedApplicationResponse:
    """Set or clear a card's title, against the `version` the client last saw."""
    raise NotImplementedError


@router.delete(
    "/tracked-applications/{tracked_application_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": (
                "The card is gone (T-30). The history entry, its documents and its exports are "
                "untouched — untracking is not deleting."
            ),
        },
        **_TRACKED_APPLICATION_NOT_FOUND,
        **_PATH_VALIDATION_ERROR,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def untrack_my_application(
    tracked_application_id: UUID,
    user_id: RequireUserDep,
    settings: SettingsDep,
    limiter: TrackingWriteRateLimiterDep,
    untrack: UntrackApplicationDep,
    db: SessionDep,
) -> Response:
    """Remove a card from the board: **204**; a second untrack is 404 (T-31)."""
    raise NotImplementedError
