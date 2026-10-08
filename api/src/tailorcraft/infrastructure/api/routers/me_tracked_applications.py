"""A signed-in user's application board: the board itself and the cards on it (slice 3.1, technical
plan §3 and §4, ADR-0029, ADR-0024 amendment (a)).

Built red-first (sdlc.md §2): **SKELETON** (T20 — real paths, real schemas, every documented status
in each `responses=` map), **RED** (T21/T22, `qa`), **GREEN** (T23 — the limiter, the value-object
construction, the domain-error translation, the in-handler commit).

**Every write, one order:** the limiter (before anything touches the database) → the domain values
built here, at the boundary (`ApplicationTitle`; its `InvalidApplicationTitle` is a 422
`validation_error` with a fixed sentence) → the use case, which publishes its own events → the wire
shape read off the aggregate → **the commit** → `no-store`. A domain refusal goes through
`domain_error_to_http_exception`; a `SQLAlchemyError` (a read, a flush, the commit) is left to
`main.py`'s handler — the 503 `service_unavailable` and its `db.request_failed` line naming the
error's type, never its message.

**Two resources, one router.** `/api/me/board` is the read model (every card, rendered whole) and
`/api/me/tracked-applications` is the collection of cards a write touches. Both answer to the same
bearer and the same aggregate, so one module; the prefix is `/api/me` and each route spells its own
resource. Neither shares a prefix with another router, so its registration position is free.

**One credential: the bearer** (`require_user`); a `__Host-tc_guest` cookie is ignored and nothing
here reads it. **No transfer route** (AC-22): the AST scan's exception set stays exactly
`{POST /api/me/guest-work/claim}`.

**Every write** passes the tracking limiter first (`TrackingWriteRateLimiterDep`: `user` scope, one
600/h budget shared by the four writes, **fails open** — T-26, T-27). The board's `GET` is unlimited.

**The unit of work ends in the handler** (CLAUDE.md, the FastAPI 0.141 footgun: a dependency's
teardown runs after the response is sent), so a failed commit is a 503 and never a 2xx claiming a
write that did not land (T-28). Every response the handler builds carries `Cache-Control: no-store`.

**Logging**: ids only — never a title, a posting's title or URL, a CV label (Constitution §8).
"""

from __future__ import annotations

from typing import Annotated, Any, Final, NoReturn
from uuid import UUID

import structlog
from fastapi import APIRouter, Body, HTTPException, Response, status

from tailorcraft.application.tracking.move_tracked_application import (
    MoveTrackedApplicationCommand,
)
from tailorcraft.application.tracking.retitle_tracked_application import (
    RetitleTrackedApplicationCommand,
)
from tailorcraft.application.tracking.track_application import TrackApplicationCommand
from tailorcraft.application.tracking.untrack_application import UntrackApplicationCommand
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.posting.value_objects import PostingSource
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.domain.tracking.board import ApplicationBoard, BoardCard
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import ApplicationTitle, TrackedApplicationId
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
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    RATE_LIMITED,
    REQUEST_TOO_LARGE,
    SERVICE_UNAVAILABLE,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.tracking import (
    BoardBaseCvResponse,
    BoardCardResponse,
    BoardPostingResponse,
    BoardResponse,
    BoardRunResponse,
    MoveTrackedApplicationRequest,
    RetitleTrackedApplicationRequest,
    TrackApplicationRequest,
    TrackedApplicationResponse,
)
from tailorcraft.infrastructure.rate_limit import RedisFixedWindowRateLimiter
from tailorcraft.infrastructure.settings import Settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/me", tags=["tracking"])

_NO_STORE: Final = "no-store"
_TRACKED_PATH: Final = "/api/me/tracked-applications"

# 2.1's line for a valid token whose account is gone (`/me`, `saved_base_cvs`): the id, nothing else.
EVENT_USER_MISSING: Final = "identity.user_missing"

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


# --------------------------------------------------------------------------------------------------
# Helpers — the steps every write shares, each written once.
# --------------------------------------------------------------------------------------------------


async def _enforce_write_limit(
    limiter: RedisFixedWindowRateLimiter, user_id: UserId, settings: Settings
) -> None:
    """The tracking write limiter: `user` scope at `tracking_write_rate_limit_per_hour` (600/h, one
    budget for the four writes), **fails open** (built with `fail_open=True`), or 429
    `rate_limited` + `Retry-After` (T-26, T-27). Keyed on the user **id**, never an email; called
    before anything touches the database, so a refused write changes nothing."""
    decision = await limiter.check(
        "user", str(user_id.value), settings.tracking_write_rate_limit_per_hour
    )
    if not decision.allowed:
        retry_after = decision.retry_after_seconds
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"Too many changes to your board. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after), "Cache-Control": _NO_STORE},
        )


def _title(raw: str | None) -> ApplicationTitle | None:
    """The wire's nullable string as the domain's value — built **here**, at the boundary, so every
    refusal (empty after trimming, over 120, a control character) is `InvalidApplicationTitle`, the
    422 `validation_error` with a fixed sentence. The title itself is never echoed (T-16)."""
    return ApplicationTitle(raw) if raw is not None else None


def _refuse(exc: DomainError, user_id: UserId) -> NoReturn:
    """A domain refusal as this router's `HTTPException`, raised `from None` so the frame holding the
    request body is unreachable from a report. `UserNotFound` (a valid token for an erased account,
    AC-28) is 2.1's 401 `not_signed_in`, with 2.1's line: the id, nothing else. A refusal the handler
    raises is a response it builds, so it carries `no-store` too — two of them name account data
    (`tracked_application_id`, `current_version`); 2.4's claim does the same."""
    if isinstance(exc, UserNotFound):
        log.info(EVENT_USER_MISSING, user_id=str(user_id.value))
    refusal = domain_error_to_http_exception(exc)
    refusal.headers = {**(refusal.headers or {}), "Cache-Control": _NO_STORE}
    raise refusal from None


def _card_to_response(card: TrackedApplication) -> TrackedApplicationResponse:
    """A card as a write returns it: the seven keys of AC-21, read off the aggregate. Called
    **before** the commit, `saved_base_cvs`'s order, so no attribute read can follow an expiry."""
    return TrackedApplicationResponse(
        id=card.id.value,
        tailoring_run_id=card.tailoring_run_id.value,
        stage=card.stage,
        title=card.title.value if card.title is not None else None,
        tracked_at=card.tracked_at,
        stage_changed_at=card.stage_changed_at,
        version=card.version,
    )


def _board_card_to_response(card: BoardCard) -> BoardCardResponse:
    """One read-model row on the wire. The posting's `source` is a plain `str` in `domain/tracking`
    (it may not name `posting`'s type); it becomes `PostingSource` again here, where the two contexts
    may meet — and a value the enum does not know would be a loud `ValueError`, not a silent string."""
    return BoardCardResponse(
        id=card.id.value,
        tailoring_run_id=card.tailoring_run_id.value,
        stage=card.stage,
        title=card.title,
        tracked_at=card.tracked_at,
        stage_changed_at=card.stage_changed_at,
        version=card.version,
        run=(
            BoardRunResponse(requested_at=card.run.requested_at, edited=card.run.edited)
            if card.run is not None
            else None
        ),
        posting=(
            BoardPostingResponse(
                job_posting_id=card.posting.job_posting_id,
                source=PostingSource(card.posting.source),
                title=card.posting.title,
                source_url=card.posting.source_url,
                preview=card.posting.preview,
            )
            if card.posting is not None
            else None
        ),
        base_cv=(
            BoardBaseCvResponse(
                base_cv_id=card.base_cv.base_cv_id,
                label=card.base_cv.label,
                original_filename=card.base_cv.original_filename,
            )
            if card.base_cv is not None
            else None
        ),
    )


def _board_to_response(board: ApplicationBoard) -> BoardResponse:
    return BoardResponse(items=[_board_card_to_response(card) for card in board.cards])


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
    """The whole board, one statement (ADR-0024 amendment (a)). No limiter.

    Writes nothing and commits anyway — 2.1's `/me` reason: the unit of work ends in the handler, so
    `get_session`'s teardown never runs a statement after the response is on the wire."""
    try:
        board = await show_board(user_id)
    except DomainError as exc:
        _refuse(exc, user_id)

    wire = _board_to_response(board)
    await db.commit()
    response.headers["Cache-Control"] = _NO_STORE
    return wire


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
    await _enforce_write_limit(limiter, user_id, settings)
    try:
        command = TrackApplicationCommand(
            user_id=user_id,
            tailoring_run_id=TailoringRunId(body.tailoring_run_id),
            stage=body.stage,
            title=_title(body.title),
        )
        card = await track(command)
    except DomainError as exc:
        _refuse(exc, user_id)

    wire = _card_to_response(card)
    await db.commit()
    response.headers["Location"] = f"{_TRACKED_PATH}/{wire.id}"
    response.headers["Cache-Control"] = _NO_STORE
    return wire


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
    await _enforce_write_limit(limiter, user_id, settings)
    try:
        card = await move(
            MoveTrackedApplicationCommand(
                user_id=user_id,
                id=TrackedApplicationId(tracked_application_id),
                stage=body.stage,
                expected_version=body.version,
            )
        )
    except DomainError as exc:
        _refuse(exc, user_id)

    wire = _card_to_response(card)
    await db.commit()
    response.headers["Cache-Control"] = _NO_STORE
    return wire


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
    await _enforce_write_limit(limiter, user_id, settings)
    try:
        card = await retitle(
            RetitleTrackedApplicationCommand(
                user_id=user_id,
                id=TrackedApplicationId(tracked_application_id),
                title=_title(body.title),
                expected_version=body.version,
            )
        )
    except DomainError as exc:
        _refuse(exc, user_id)

    wire = _card_to_response(card)
    await db.commit()
    response.headers["Cache-Control"] = _NO_STORE
    return wire


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
    await _enforce_write_limit(limiter, user_id, settings)
    try:
        await untrack(
            UntrackApplicationCommand(
                user_id=user_id, id=TrackedApplicationId(tracked_application_id)
            )
        )
    except DomainError as exc:
        _refuse(exc, user_id)

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": _NO_STORE})
