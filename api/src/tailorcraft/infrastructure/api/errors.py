"""`DomainError` -> `HTTPException` translation for the `intake`/`identity` HTTP surface.

The domain never raises `HTTPException` and never carries a status code (CLAUDE.md, ADR-0004) — this
is the one module that assigns one, for every `DomainError` this slice's use cases can raise.

**Why this lives in its own module instead of only inside `routers/intake.py`.** `deps.py`'s
`require_guest_session` cookie dependency needs the exact same 401 shape `routers/intake.py` uses for
a `GuestSessionExpired`/`GuestSessionNotFound` raised deeper inside a use case — and a dependency's
own exception cannot be caught by a router handler's `try/except`, because FastAPI raises it before
the handler body ever runs. Sharing one mapping here, imported by both, is what keeps the two 401s
from drifting into two different messages for the same failure. This module is still squarely
"infrastructure" (it imports `fastapi` and the domain's error types, nothing the other direction) —
the architectural rule it satisfies is "the domain doesn't know HTTP exists", not "the translation
must be textually inside one specific file".
"""

from __future__ import annotations

from fastapi import HTTPException, status

from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.intake.errors import BaseCvNotFound, InvalidFilename, TooManyBaseCvs
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.shared.files import FileStoreUnavailable

# Shared by `deps.py::require_guest_session` (which never reaches a use case at all — it raises
# straight from a missing/unknown cookie) and this module's own mapping for a `GuestSessionExpired`/
# `GuestSessionNotFound` raised *inside* a use case (F-19's defense-in-depth path) — both must read
# identically to a client, so there is exactly one place either can drift from the other: nowhere.
GUEST_SESSION_EXPIRED_DETAIL = {
    "code": "guest_session_expired",
    "message": "Your session has expired or could not be found. Please try again.",
}


def domain_error_to_http_exception(exc: DomainError) -> HTTPException:
    """Map one `DomainError` to the `HTTPException` the API returns for it.

    Every branch here is a row of `docs/specs/intake-base-cv-upload/feature-spec.md`'s failure
    contract. An exception type that reaches the final `raise exc` is a bug — a use case raising
    something this slice never planned a status code for — so it is re-raised unchanged rather than
    smoothed over into a fake, misleading 4xx; the app's own unhandled-exception path (a real 500)
    is the honest response to a failure this mapping does not know about.
    """
    if isinstance(exc, GuestSessionExpired | GuestSessionNotFound):
        return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=GUEST_SESSION_EXPIRED_DETAIL)

    if isinstance(exc, BaseCvNotFound):
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "code": "base_cv_not_found",
                "message": "No base CV was found with that id.",
            },
        )

    if isinstance(exc, TooManyBaseCvs):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "too_many_base_cvs",
                "message": "You have reached the maximum number of base CVs for this session.",
            },
        )

    if isinstance(exc, InvalidFilename):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_filename",
                "message": "The uploaded file's name is not valid.",
            },
        )

    if isinstance(exc, FileStoreUnavailable):
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "storage_unavailable",
                "message": "File storage is temporarily unavailable. Please try again shortly.",
            },
        )

    raise exc
