"""`admin.content_access`: one line per admin content request (slice 4.3 §0.7, lifted in 4.4).

Shared by every admin router that reads or deletes another principal's content. Each passes the
key naming its target — `"target_user_id"` for an account (4.3), `"target_guest_session_id"` for a
guest session (4.4) — so one log search over `admin.content_access` finds both.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from types import TracebackType
from typing import Final, Literal
from uuid import UUID

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.domain.identity.user import User

log = structlog.get_logger(__name__)

EVENT_ADMIN_CONTENT_ACCESS: Final = "admin.content_access"


@dataclass(slots=True)
class AdminAccess:
    """One `admin.content_access` line per request (§0.7): two ids, the item's id (absent on a
    list) and three closed words — never a body, a filename, a label, a title or an email.

    Used as `with AdminAccess(...) as access:` around a handler's body, so a `SQLAlchemyError` that
    leaves it unlogged (the global 503) still writes its one `refused` line (AC-20). The exception
    is never swallowed and its message never logged; a second `log` is a no-op."""

    actor: InitVar[User]
    target: UUID
    kind: Literal["base_cv", "job_posting", "tailoring_run", "export_job", "history"]
    action: Literal["list", "read", "download", "delete"]
    item_id: UUID | None = None
    target_key: str = field(kw_only=True)
    _actor_id: str = field(init=False)
    _logged: bool = field(default=False, init=False)

    def __post_init__(self, actor: User) -> None:
        # Read now: a handler's rollback expires the loaded `User`, and a lazy load on an
        # `AsyncSession` is `MissingGreenlet`.
        self._actor_id = str(actor.id.value)

    def __enter__(self) -> AdminAccess:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if isinstance(exc, SQLAlchemyError):
            self.log("refused")

    def log(self, outcome: Literal["ok", "not_found", "refused"]) -> None:
        if self._logged:
            return
        self._logged = True
        fields: dict[str, str] = {
            "actor_id": self._actor_id,
            self.target_key: str(self.target),
            "kind": self.kind,
            "action": self.action,
            "outcome": outcome,
        }
        if self.item_id is not None:
            fields["item_id"] = str(self.item_id)
        log.info(EVENT_ADMIN_CONTENT_ACCESS, **fields)

    def refuse(self, http: HTTPException) -> HTTPException:
        """Log the refusal (a 404 is `not_found`, anything else `refused`) and hand it back."""
        self.log("not_found" if http.status_code == status.HTTP_404_NOT_FOUND else "refused")
        return http


EVENT_ADMIN_RETENTION_ACTION: Final = "admin.retention_action"

RetentionActionName = Literal[
    "guest_list",
    "guest_overview",
    "guest_erase",
    "storage_overview",
    "purge_preview",
    "purge_run",
    "sweep_preview",
    "sweep_run",
]


@dataclass(slots=True)
class RetentionAction:
    """One `admin.retention_action` line per request (slice 4.4, AC-29): the actor, a closed action
    word, a closed outcome, the guest session's id where there is one, and **counts only**.

    `AdminAccess`'s sibling and its shape: a `SQLAlchemyError` that leaves the block unlogged (the
    global 503) still writes its one `refused` line; the exception is never swallowed and its
    message never logged; a second `log` is a no-op."""

    actor: InitVar[User]
    action: RetentionActionName
    guest_session_id: UUID | None = None
    _actor_id: str = field(init=False)
    _logged: bool = field(default=False, init=False)

    def __post_init__(self, actor: User) -> None:
        # Read now, as `AdminAccess` does: a rollback expires the loaded `User`.
        self._actor_id = str(actor.id.value)

    def __enter__(self) -> RetentionAction:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if isinstance(exc, SQLAlchemyError):
            self.log("refused")

    def log(self, outcome: Literal["ok", "not_found", "refused", "failed"], **counts: int) -> None:
        if self._logged:
            return
        self._logged = True
        fields: dict[str, str | int] = {
            "actor_id": self._actor_id,
            "action": self.action,
            "outcome": outcome,
        }
        if self.guest_session_id is not None:
            fields["guest_session_id"] = str(self.guest_session_id)
        log.info(EVENT_ADMIN_RETENTION_ACTION, **fields, **counts)

    def refuse(self, http: HTTPException) -> HTTPException:
        """Log the refusal — a 404 is `not_found`, a 5xx `failed`, anything else `refused` — and
        hand it back."""
        if http.status_code == status.HTTP_404_NOT_FOUND:
            self.log("not_found")
        elif http.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            self.log("failed")
        else:
            self.log("refused")
        return http
