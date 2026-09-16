"""`DomainError` -> `HTTPException` translation for this API's whole HTTP surface — `identity`,
`intake`, `posting` and `tailoring`.

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
from tailorcraft.domain.tailoring.errors import (
    BaseCvNotReadyForTailoring,
    EmptyTailoredDocument,
    InvalidTailoredDocument,
    TailoredDocumentTooLong,
    TailoredDocumentTooShort,
    TailoredDocumentVersionConflict,
    TailoringAlreadyRunning,
    TailoringNotQueued,
    TailoringRunConcurrentlyModified,
    TailoringRunNotEditable,
    TailoringRunNotFound,
    TooManyTailoringRuns,
)

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

    # -- tailoring (slice 1.3) -----------------------------------------------------------------
    # Again a branch in the SAME function, for the same reason: `deps.py` and all three routers
    # share one mapping, and one mapping is what keeps three 401s from becoming three messages.
    #
    # Note which tailoring errors are NOT here, and that the absences are structural rather than
    # forgotten. Every `TailoringFailed` subclass (`LlmUnavailable`, `LlmRefused`, `LlmTimedOut`, …)
    # is caught by `ExecuteTailoringRun` inside the worker and recorded on the aggregate as
    # `failed` + a `failure_reason`; it never reaches an HTTP boundary, and the client learns about
    # it from a **200** on the next poll (AC-12). The same goes for the aggregate's transition
    # guards (`TailoringAlreadyStarted`, `TailoringNotRunning`, `TailoringAlreadyDecided`) — they
    # live entirely inside the task. Reaching the `raise exc` floor below with one of those is a
    # genuine bug, and a real 500 is the honest answer to it, exactly as this function's docstring
    # says. (The document value objects' errors used to be on this list; since 1.4 the `PUT`
    # router constructs those value objects from the client's text, so they are mapped below.)

    if isinstance(exc, TailoringRunNotFound):
        # G-29: also what `GetTailoringRunForSession` raises (`from TailoringRunNotOwnedBySession`)
        # for a run that exists but belongs to someone else. The two are indistinguishable on the
        # wire on purpose — a 403 would confirm that a guessed id is real.
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "code": "tailoring_run_not_found",
                "message": "We couldn't find that tailoring run.",
            },
        )

    if isinstance(exc, BaseCvNotReadyForTailoring):
        # G-8. 409 rather than 422: the request was well-formed and named a CV the caller really
        # owns — it is the *state* of that CV that conflicts with what was asked for, and the fix is
        # to upload a different file rather than to correct the request.
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "base_cv_not_extracted",
                "message": (
                    "We couldn't read that CV, so there's nothing to tailor. Upload a different "
                    "file."
                ),
            },
        )

    if isinstance(exc, TailoringAlreadyRunning):
        # G-9, and the one error in this module whose body carries a THIRD field beyond the
        # envelope's `code`/`message`. No new mechanism was needed for that: `main.py`'s
        # `HTTPException` handler renders `{"error": exc.detail}` whenever the detail dict carries a
        # `code` and a `message`, so any extra key rides along untouched. `ErrorResponse` in
        # `schemas/intake.py` documents the two guaranteed fields and does not forbid others.
        #
        # The id is the point rather than a nicety: without it the browser can only tell the user
        # "something is already running" and offer them nothing to look at, and the obvious next
        # click is another POST. With it, the client attaches its poller to the run that is already
        # paying for itself. It is stringified here because this dict is JSON-serialized directly by
        # `JSONResponse`, which has no encoder for a `UUID`.
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "tailoring_already_running",
                "message": "You already have a tailoring run in progress.",
                "active_tailoring_run_id": str(exc.active_run_id.value),
            },
        )

    if isinstance(exc, TooManyTailoringRuns):
        # G-10. The message names the limit it hit, from the error's own payload, so the number
        # lives in one place (the settings object the use case read) rather than being repeated as a
        # literal in a sentence that would then age separately from the rule.
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "too_many_tailoring_runs",
                "message": (
                    f"You've reached the limit of {exc.limit} tailoring runs for this session."
                ),
            },
        )

    if isinstance(exc, TailoringNotQueued):
        # G-14. 503 and not 500: nothing about the request was wrong, the broker was unreachable,
        # and trying again later is the right advice. By the time this is raised the run row has
        # been committed and re-recorded as `failed`/`not_queued` (the router's second transaction),
        # so the user is not left polling a run that can never run.
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "queue_unavailable",
                "message": "We couldn't start your tailoring run. Please try again.",
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

    # -- tailoring, the editor's write path (slice 1.4, ADR-0015) --------------------------------
    # The four document value-object errors cross the HTTP boundary here for the FIRST time. In 1.3
    # they were raised only inside `parse_tailoring_response` on the model's output and recorded on
    # the run; in 1.4 the router constructs `TailoredCv`/`CoverLetter` from the *client's* text and
    # lets them reach this mapping (E-10 … E-12). One code, `document_invalid`, and a fixed
    # `problem` label per type: the client branches on the label (its copy names the floor or the
    # ceiling per kind, which it already knows), never on a sentence.
    #
    # **The `message` is a fixed sentence and never `str(exc)`.** Today those four messages carry
    # only counts and limits — checked, not assumed — but the rule is about what a message *could*
    # carry: this dict is the response body, and the text the value object refused is the user's
    # own CV. A fixed sentence cannot leak it whatever a future message includes (AC-16, AC-19).

    if isinstance(exc, EmptyTailoredDocument):
        return _document_invalid("empty", "The document is empty.")

    if isinstance(exc, TailoredDocumentTooShort):
        return _document_invalid("too_short", "The document is too short to save.")

    if isinstance(exc, TailoredDocumentTooLong):
        return _document_invalid("too_long", "The document is too long to save.")

    if isinstance(exc, InvalidTailoredDocument):
        return _document_invalid(
            "invalid_characters", "The document contains characters that cannot be stored."
        )

    if isinstance(exc, TailoringRunNotEditable):
        # E-7. 409, not 422: the request was well-formed and named a run the caller owns — it is
        # the run's *state* that conflicts with what was asked for, exactly `base_cv_not_extracted`'s
        # reasoning one aggregate over. The body carries `status` the way `tailoring_already_running`
        # carries `active_tailoring_run_id`: the client tells "still working, keep polling" apart
        # from "there is nothing to edit" without a second read.
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "tailoring_run_not_editable",
                "message": "This run can't be edited.",
                "status": exc.status.value,
            },
        )

    if isinstance(exc, TailoredDocumentVersionConflict):
        # E-8 — the aggregate's own compare: the editor was shown an older `version`. The body
        # carries the number the run is actually at, so the client can refetch and compare rather
        # than merely being told "no".
        return _document_version_conflict(current_version=exc.current_version)

    if isinstance(exc, TailoringRunConcurrentlyModified):
        # E-9 — the repository's translation of `StaleDataError`: two writers passed the aggregate's
        # compare and the second `UPDATE … WHERE version = :seen` matched no row. Same code as E-8,
        # because it is the same thing to the person holding the tab ("your copy is stale, refetch
        # and compare"), but `current_version` is **`null`**: the aggregate this request holds IS
        # the stale copy, and the true number lives in a row it has just been told it does not
        # have. Inventing one would be a lie the client would send straight back as
        # `expected_version`.
        return _document_version_conflict(current_version=None)

    raise exc


def _document_invalid(problem: str, message: str) -> HTTPException:
    """422 `document_invalid` with a fixed `problem` label (E-10 … E-12) — one builder so the four
    branches above cannot drift into four envelope shapes."""
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": "document_invalid", "message": message, "problem": problem},
    )


def _document_version_conflict(*, current_version: int | None) -> HTTPException:
    """409 `document_version_conflict` from either of its two sources (E-8 with the number, E-9 with
    `null`) — one builder, one code, one message, so the client has one branch to write."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "document_version_conflict",
            "message": "This document was changed elsewhere. Reload the latest version.",
            "current_version": current_version,
        },
    )


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
