"""The saved-CV wire shape and its unlink warning, shared by every route that answers with a saved CV
(slice 4.3, technical plan §0.5, lifted from 2.2's `saved_base_cvs.py`): the user's own
`/api/me/base-cvs` and the administrator's `/api/admin/users/{id}/base-cvs`.

Pure functions, no I/O. Two sources, one key set (2.2's AC-20).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import structlog

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.saved_base_cv_summary import SavedBaseCvSummary
from tailorcraft.infrastructure.api.routers._upload import failure_message
from tailorcraft.infrastructure.api.schemas.intake import SavedBaseCvResponse
from tailorcraft.infrastructure.settings import Settings

log = structlog.get_logger(__name__)

EVENT_FILE_UNLINK_FAILED: Final = "intake.saved_base_cv_file_unlink_failed"


def summary_to_response(summary: SavedBaseCvSummary, settings: Settings) -> SavedBaseCvResponse:
    """The list's entry, from the read model (technical plan §3 amendment): no text was loaded."""
    return SavedBaseCvResponse(
        id=summary.id.value,
        label=summary.label.value if summary.label is not None else None,
        original_filename=summary.original_filename.value,
        content_type=summary.content_type,
        size_bytes=summary.size_bytes,
        status=summary.status,
        character_count=summary.character_count,
        failure_reason=summary.failure_reason,
        failure_message=(
            failure_message(summary.failure_reason, settings)
            if summary.failure_reason is not None
            else None
        ),
        uploaded_at=summary.uploaded_at,
    )


def aggregate_to_response(cv: BaseCv, settings: Settings) -> SavedBaseCvResponse:
    """After an upload or a rename, from the aggregate the use case returned or saved."""
    return SavedBaseCvResponse(
        id=cv.id.value,
        label=cv.label.value if cv.label is not None else None,
        original_filename=cv.original_filename.value,
        content_type=cv.content_type,
        size_bytes=cv.size_bytes,
        status=cv.status,
        character_count=(
            cv.extracted_text.character_count if cv.extracted_text is not None else None
        ),
        failure_reason=cv.failure_reason,
        failure_message=(
            failure_message(cv.failure_reason, settings) if cv.failure_reason is not None else None
        ),
        uploaded_at=cv.uploaded_at,
    )


def log_unlink_failure(base_cv_id: UUID, user_id: UserId, error_type: str | None) -> None:
    """S-24 / R-5: the row is gone, the bytes are not — an orphan for the operator's sweep, and
    this line is how the operator learns there is one. Ids and a class name: never the key, never
    the path, never the exception's message."""
    log.warning(
        EVENT_FILE_UNLINK_FAILED,
        base_cv_id=str(base_cv_id),
        user_id=str(user_id.value),
        error_type=error_type,
    )
