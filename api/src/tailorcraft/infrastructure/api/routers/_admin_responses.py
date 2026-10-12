"""What every `/api/admin` router answers with: `no-store`, refusals and the firewall's OpenAPI rows
(slice 4.3, technical plan §0.4, lifted from 4.2's `admin_users.py`).

Shared by `admin_users.py` (account actions) and `admin_content.py` (an account's content), so the
two cannot drift on how a missing target or a refused action reads on the wire.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import HTTPException, Response, status

from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    SERVICE_UNAVAILABLE,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

NO_STORE: Final = {"Cache-Control": "no-store"}

ACCOUNT_DISABLED: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "account_disabled — the administrator's own account is disabled.",
    },
}

USER_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "user_not_found — no account with that id (including one erased concurrently)."
        ),
    },
}

FIREWALL: dict[int | str, dict[str, Any]] = {
    **NOT_SIGNED_IN,
    **ACCOUNT_DISABLED,
    **SERVICE_UNAVAILABLE,
}


def set_no_store(response: Response) -> None:
    response.headers.update(NO_STORE)


def refusal(exc: DomainError) -> HTTPException:
    """`exc` through the global map, carrying `no-store` (`main.py` renders these headers)."""
    http = domain_error_to_http_exception(exc)
    http.headers = {**(http.headers or {}), **NO_STORE}
    return http


def user_not_found() -> HTTPException:
    """404 `user_not_found` for a missing **target** (4.2's plan §0.7, 4.3's §0.4).

    Not the global map on purpose: `errors.py` turns `UserNotFound`/`AccountNotFound` into 401
    `not_signed_in`, which means "*your* account is gone" and makes the client sign the
    administrator out. Here the account that is gone is someone else's. The *actor's* own
    `UserNotFound` never reaches a handler: `require_admin` answers it with the 401 first.
    """
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"code": "user_not_found", "message": "No account was found with that id."},
        headers=dict(NO_STORE),
    )
