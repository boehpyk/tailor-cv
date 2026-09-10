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

from typing import assert_never

from fastapi import HTTPException, status

from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.intake.errors import BaseCvNotFound, InvalidFilename, TooManyBaseCvs
from tailorcraft.domain.posting.errors import (
    EmptyJobPostingText,
    InvalidSourceUrl,
    JobPostingFetchFailed,
    JobPostingNotFound,
    JobPostingTextTooLong,
    JobPostingTextTooShort,
    TooManyJobPostings,
)
from tailorcraft.domain.posting.value_objects import FetchFailureReason
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

    # -- posting (slice 1.2) -------------------------------------------------------------------
    # A branch in the SAME function rather than a second translation module, for the reason this
    # module's docstring already gives: `deps.py` and both routers share one mapping, and one
    # mapping is what keeps two 401s from drifting into two messages.

    if isinstance(exc, JobPostingNotFound):
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "code": "job_posting_not_found",
                "message": "No job posting was found with that id.",
            },
        )

    if isinstance(exc, TooManyJobPostings):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "too_many_job_postings",
                "message": (
                    "You have reached the maximum number of job postings for this session. "
                    "Delete one, or start a new session."
                ),
            },
        )

    if isinstance(exc, InvalidSourceUrl):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_source_url",
                "message": (
                    "That does not look like a job posting link. Paste an http:// or https:// "
                    "address, or paste the description text instead."
                ),
            },
        )

    if isinstance(exc, EmptyJobPostingText | JobPostingTextTooShort):
        # One code for two domain errors on purpose: "you pasted nothing" and "you pasted three
        # words" are the same problem to the person reading the message, and splitting them would
        # make the client branch on a distinction it cannot act on differently.
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "posting_text_too_short",
                "message": (
                    "That job description is too short to work with. Paste the full posting — "
                    "at least 100 characters."
                ),
            },
        )

    if isinstance(exc, JobPostingTextTooLong):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "posting_text_too_long",
                "message": (
                    "That job description is longer than 30,000 characters. Paste the part that "
                    "matters — the responsibilities and requirements."
                ),
            },
        )

    if isinstance(exc, JobPostingFetchFailed):
        # The one branch that maps a whole family. `_fetch_failure_to_http` is exhaustive over
        # `FetchFailureReason` and `assert_never` proves it, so a reason added later is a type error
        # here rather than an unmapped 500 discovered by a user.
        return _fetch_failure_to_http(exc.reason)

    if isinstance(exc, FileStoreUnavailable):
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "storage_unavailable",
                "message": "File storage is temporarily unavailable. Please try again shortly.",
            },
        )

    raise exc


def _fetch_failure_to_http(reason: FetchFailureReason) -> HTTPException:
    """Map a fetch failure to its status and `code`.

    **Every message names the paste fallback, and that is FR-2 rather than politeness** (AC-13). The
    product's answer to a link we cannot read is a message *and a path forward*; the React surface
    turns each of these into a button that switches the input to paste mode with the URL still
    visible. A message that said "try again" would be advice to repeat something that will fail the
    same way — Cloudflare will refuse us the second time too.

    The statuses split on whose fault it is. `fetch_blocked` and `invalid_source_url` are 422,
    because the request named a target we will not fetch. Everything else is 502 or 504: we tried, and
    the far end failed us — the client's request was well-formed and re-sending it unchanged is not
    the fix, which is exactly what a 4xx would imply.
    """
    if reason is FetchFailureReason.BLOCKED_TARGET:
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "fetch_blocked",
                "message": (
                    "We cannot fetch that address. Open the posting in your browser and paste the "
                    "description instead."
                ),
            },
        )
    if reason is FetchFailureReason.UNREACHABLE:
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "source_unreachable",
                "message": (
                    "We could not reach that site. Check the link, or paste the description "
                    "instead."
                ),
            },
        )
    if reason is FetchFailureReason.TIMED_OUT:
        return HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "code": "source_timed_out",
                "message": (
                    "That site took too long to respond. Paste the description instead and you "
                    "will not have to wait for it."
                ),
            },
        )
    if reason is FetchFailureReason.REJECTED:
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "source_rejected",
                "message": (
                    "That site refused our request — many job boards block automated readers. "
                    "Paste the description instead."
                ),
            },
        )
    if reason is FetchFailureReason.TOO_MANY_REDIRECTS:
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "source_too_many_redirects",
                "message": (
                    "That link redirected too many times. Open it in your browser and paste the "
                    "description instead."
                ),
            },
        )
    if reason is FetchFailureReason.RESPONSE_TOO_LARGE:
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "source_response_too_large",
                "message": (
                    "That page is too large for us to read. Paste the description instead."
                ),
            },
        )
    if reason is FetchFailureReason.NOT_HTML:
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "source_not_html",
                "message": (
                    "That link is not a web page we can read — a PDF or an image, perhaps. Paste "
                    "the description instead."
                ),
            },
        )
    if reason is FetchFailureReason.NO_READABLE_TEXT:
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "source_no_readable_text",
                "message": (
                    "We reached that page but found no readable description — it may need "
                    "JavaScript to load. Paste the description instead."
                ),
            },
        )
    if reason is FetchFailureReason.TEXT_TOO_LONG:
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "source_text_too_long",
                "message": (
                    "That page holds more than 30,000 characters of text. Paste the part that "
                    "matters — the responsibilities and requirements."
                ),
            },
        )
    if reason is FetchFailureReason.FETCHER_ERROR:
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "fetcher_error",
                "message": (
                    "Something went wrong reading that link. Paste the description instead."
                ),
            },
        )
    # Exhaustive over `FetchFailureReason`; mypy narrows to `Never` here only if every member above
    # is handled. Add a reason without a branch and this becomes a type error naming it, rather than
    # an `HTTPException` that never gets built and a 500 a user finds for us.
    assert_never(reason)
