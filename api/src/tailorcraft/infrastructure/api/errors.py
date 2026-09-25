"""`DomainError` -> `HTTPException` translation for this API's whole HTTP surface — `identity`,
`intake`, `posting`, `tailoring` and `export`.

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

from tailorcraft.domain.export.errors import (
    DocumentRenderFailed,
    DocumentRenderTimedOut,
    ExportFormatNotInline,
    ExportFormatNotQueued,
    ExportJobNotFound,
    ExportNotQueued,
    ExportNotReady,
    TailoringRunNotExportable,
    TooManyExportJobs,
)
from tailorcraft.domain.identity.errors import (
    AccessTokenInvalid,
    EmailAlreadyRegistered,
    GuestSessionExpired,
    GuestSessionNotFound,
    InvalidCredentials,
    InvalidEmailAddress,
    LoginNotFound,
    PasswordHashingFailed,
    RefreshInProgress,
    RefreshTokenReused,
    UserNotFound,
    WeakPassword,
)
from tailorcraft.domain.identity.value_objects import WeakPasswordReason
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
from tailorcraft.domain.shared.files import FileStoreUnavailable, StoredFileMissing
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

# -- identity (slice 2.1) ------------------------------------------------------------------------
# Shared details, for `GUEST_SESSION_EXPIRED_DETAIL`'s reason: a dependency (`require_user`,
# `require_trusted_origin`) and a route that raises the same refusal from its own body (a refresh with
# no cookie, I-19) must read identically, and one constant is the only way that stays true.

NOT_SIGNED_IN_DETAIL = {
    "code": "not_signed_in",
    "message": "You are not signed in.",
}
"""401 for every "this refresh cookie names no live login" (I-19, I-20, I-21, I-27) and for a valid
access token whose user is gone (I-39). One code: after a revocation, "revoked" and "never existed"
are indistinguishable by design (ADR-0020)."""

INVALID_ACCESS_TOKEN_DETAIL = {
    "code": "invalid_access_token",
    "message": "Your session needs to be refreshed.",
}
"""401 for every refused bearer token (I-32 … I-38, I-40). **Never says why** (AC-34): a forger learns
nothing about which check caught them; the reason goes to the log line only."""

WWW_AUTHENTICATE_INVALID_TOKEN = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
"""RFC 6750 §3's challenge, on every `invalid_access_token` (AC-34)."""

ORIGIN_NOT_ALLOWED_DETAIL = {
    "code": "origin_not_allowed",
    "message": "This request did not come from TailorCraft.",
}
"""403 from `require_trusted_origin` (AC-25, I-28). Not a `DomainError`: an `Origin` header is an HTTP
fact the domain has no word for."""


def invalid_access_token_exception() -> HTTPException:
    """The 401 `invalid_access_token` with its `WWW-Authenticate` challenge — one builder, used by
    `deps.require_user` and by this module's `AccessTokenInvalid` branch, so the two cannot drift."""
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail=INVALID_ACCESS_TOKEN_DETAIL,
        headers=WWW_AUTHENTICATE_INVALID_TOKEN,
    )


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

    # -- identity: registration, login, refresh, access tokens (slice 2.1) ----------------------
    # The same function once more, for the module docstring's reason. The union is the specification
    # of what 2.1's use cases and adapters can raise across HTTP. Two identity errors are absent on
    # purpose: `LoginExpired` and `LoginConcurrentlyRotated` are translated inside `RefreshLogin`
    # (into `LoginNotFound` and `RefreshInProgress`) and never reach a route — reaching the floor
    # with either is a bug, and a real 500 is the honest answer to it.
    if isinstance(
        exc,
        InvalidEmailAddress
        | WeakPassword
        | EmailAlreadyRegistered
        | InvalidCredentials
        | LoginNotFound
        | UserNotFound
        | RefreshInProgress
        | RefreshTokenReused
        | AccessTokenInvalid
        | PasswordHashingFailed,
    ):
        return _identity_error_to_http(exc)

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

    # **NARROW FIRST, and this ordering is the whole branch.** `StoredFileMissing` is a *subclass*
    # of `FileStoreUnavailable` (`domain/shared/files.py` says why it is a subclass rather than a
    # sibling), so the generic 503 below would swallow it and X-47's 410 would never be reachable —
    # the identical trap `LocalFileStore.get` hit at I8, where `FileNotFoundError` is an `OSError`
    # and the existing floor caught it first. Two `isinstance` checks in the wrong order is all it
    # takes, which is why the narrow one is pinned above the wide one with this note between them.
    #
    # Mapped here rather than in `routers/export.py::download_export_file`'s own `except` (I16's
    # open decision #1) because **this slice is the codebase's first caller of `FileStorePort.get`
    # at all**: no existing response can change, there is exactly one route that can produce either
    # error today, and putting the pair one line apart in one function is what keeps the ordering
    # visible to the next reader. A second route that reads the store inherits both answers for
    # free; a handler-local `except` would have had to remember them.
    #
    # 410, not 404: the job resource is right there and answers a poll — it is the *file* that is
    # gone, and the honest next action is *Export again* rather than "that id does not exist".
    if isinstance(exc, StoredFileMissing):
        return HTTPException(
            status.HTTP_410_GONE,
            detail={
                "code": "export_file_gone",
                "message": "That file is no longer available. Export it again.",
            },
        )

    # X-48 / AC-26 **decided: the code is `storage_unavailable`, not `service_unavailable`.** The
    # spec named the generic one and this branch — already shipped, already asserted green by
    # `tailoring`'s tests — names the specific one. One condition gets one code, and the specific
    # code beats the generic: a client that sees `storage_unavailable` knows the store is the part
    # that is down, where `service_unavailable` (which this API also returns for a dead Postgres)
    # would say only "something". The spec has been corrected rather than this branch.
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

    # -- export (slice 1.5, ADR-0016 / ADR-0017) ------------------------------------------------
    # Again a branch in the SAME function, for the reason this module's docstring gives: `deps.py`
    # and all four routers share one mapping, and one mapping is what keeps four 401s from becoming
    # four messages.
    #
    # The `isinstance` union below IS the specification of what this slice's use cases can raise
    # across an HTTP boundary, which is why it is written whole at the skeleton stage while the
    # statuses and codes behind it are not: the tuple is a signature, `_export_error_to_http`'s body
    # is behaviour. I18 fills it in against the failure contract:
    #
    #   TailoringRunNotExportable -> 409 `tailoring_run_not_exportable` (+ `status`)   X-4, X-14
    #   TooManyExportJobs         -> 409 `too_many_export_jobs` (no number in the body) X-18
    #   ExportFormatNotQueued     -> 422 `validation_error`                            X-15
    #   ExportFormatNotInline     -> 422 `validation_error`                            X-1
    #   ExportJobNotFound         -> 404 `export_job_not_found`                        X-43
    #   ExportNotReady            -> 409 `export_not_ready` (+ `status`, + `failure_reason`)
    #                                                                                  X-44, X-45
    #   ExportNotQueued           -> 503 `queue_unavailable`                           X-22
    #   DocumentRenderTimedOut    -> 503 `render_timed_out`                            X-6
    #   DocumentRenderFailed      -> 500 `render_failed` — the floor, and the ONE deliberate 500
    #                                in this codebase. Order matters: the timeout is a subclass.
    #                                                                                  X-5
    #
    # **Note which two export errors are NOT in this union, and that both absences are structural.**
    # `StoredFileMissing` (X-47 -> 410) and `FileStoreUnavailable` (X-48 -> 503 `storage_unavailable`)
    # are mapped *above*, beside the `tailoring` branch that already owned the wider of the two, and
    # both of I16's open decisions are resolved there with the reasons written at the branches
    # themselves: the narrow type is pinned above the wide one, and `storage_unavailable` won over
    # `service_unavailable`.
    if isinstance(
        exc,
        TailoringRunNotExportable
        | TooManyExportJobs
        | ExportFormatNotQueued
        | ExportFormatNotInline
        | ExportJobNotFound
        | ExportNotReady
        | ExportNotQueued
        | DocumentRenderFailed,
    ):
        return _export_error_to_http(exc)

    raise exc


def _identity_error_to_http(exc: DomainError) -> HTTPException:
    """Map one `identity` `DomainError` to its status and `code` (the API contract, technical plan §4).

    **Every `message` is a fixed sentence and never `str(exc)`.** Identity is the context where an
    error is raised *about* an email or a password; the domain's messages carry reasons only (its
    module docstring), and a fixed sentence here keeps that true whatever a future message includes.
    """
    if isinstance(exc, InvalidEmailAddress):
        # I-1, I-12. One code for every `InvalidEmailReason`: which rule failed helps nobody type an
        # address, and on login it would be a statement about input shaped like an account.
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_email", "message": "That doesn't look like an email address."},
        )

    if isinstance(exc, WeakPassword):
        return _weak_password_to_http(exc)

    if isinstance(exc, EmailAlreadyRegistered):
        # I-5, I-6. Conceded enumeration at registration (OQ-1): the alternative is an account-
        # creation flow that cannot tell a person their address is already in use.
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "email_already_registered",
                "message": "An account with that email already exists. Log in instead.",
            },
        )

    if isinstance(exc, InvalidCredentials):
        # I-9, I-10 — **byte-identical for an unknown email and a wrong password** (AC-28). This
        # branch reads nothing off `exc` (it carries nothing, AC-9), so it cannot vary.
        return HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_credentials",
                "message": "That email and password don't match an account.",
            },
        )

    if isinstance(exc, LoginNotFound | UserNotFound):
        # I-20, I-21, I-27 (refresh) and I-39 (`/me`, the user row gone). Clearing the cookie on the
        # refresh path is the route's job — this mapping has no response to set it on.
        return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=NOT_SIGNED_IN_DETAIL)

    if isinstance(exc, RefreshInProgress):
        # I-23, I-25. 409 and not 429: a conflict with concurrent state, not a limit. The cookie is
        # left alone — by the time the client retries, it holds the winner's.
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "refresh_in_progress",
                "message": "Another tab is refreshing your session. Try again in a moment.",
            },
        )

    if isinstance(exc, RefreshTokenReused):
        # I-24. The login is already deleted; the route must answer this *inside* the request so the
        # deletion commits, and must clear the cookie.
        return HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "refresh_token_reused",
                "message": "For your security you've been logged out. Please log in again.",
            },
        )

    if isinstance(exc, AccessTokenInvalid):
        # I-33 … I-38. `exc.reason` is for the log line (`deps.require_user` writes it) and is never
        # read here: every refusal is the same 401, and the body never says why (AC-34).
        return invalid_access_token_exception()

    if isinstance(exc, PasswordHashingFailed):
        # I-45. The same code and message as a dead Postgres (`main.py`'s handler): to the client,
        # "the server cannot check passwords right now" and "the server is down" ask for one action.
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "The service is temporarily unavailable. Please try again.",
            },
        )

    raise exc


def _weak_password_to_http(exc: WeakPassword) -> HTTPException:
    """I-2 … I-4. **Each reason is its own wire code** (`WeakPasswordReason`'s values are the codes),
    because the user can act on the difference. The bound travels in the envelope from the object
    that applied it — `min_length` for too-short, `max_length` for too-long — so the client never
    hard-codes 12 as a rule, only as copy (technical plan §1: the policy is data)."""
    reason = exc.reason
    if reason is WeakPasswordReason.TOO_SHORT:
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": reason.value,
                "message": f"Use at least {exc.min_length} characters.",
                "min_length": exc.min_length,
            },
        )
    if reason is WeakPasswordReason.TOO_LONG:
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": reason.value,
                "message": f"Use at most {exc.max_length} characters.",
                "max_length": exc.max_length,
            },
        )
    if reason is WeakPasswordReason.MATCHES_EMAIL:
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": reason.value,
                "message": "Your password can't be your email address.",
            },
        )
    assert_never(reason)


def _export_error_to_http(exc: DomainError) -> HTTPException:
    """Map one `export` `DomainError` to its status and `code` (X-4 … X-22, X-43 … X-45).

    Split out of `domain_error_to_http_exception` for length alone — the union that reaches it is
    declared at the call site and is the specification of what this slice's use cases can raise
    across an HTTP boundary. Two orderings inside are load-bearing and neither is arbitrary:

    * `DocumentRenderTimedOut` is tested **before** its `DocumentRenderFailed` base. The subclass
      ordering is the whole difference between a 503 that invites a retry that can work (X-6) and
      the 500 that admits a bug (X-5).
    * The floor is `DocumentRenderFailed` -> **500 `render_failed`**, and it is the one deliberate
      500 in this codebase. Only the *inline* path can reach it: `RenderExportJob` catches the same
      family and records it on a committed row, so by the time a render failure crosses HTTP there
      is no row, nothing was spent, and nothing about the request was wrong. Not a 4xx (the client
      sent a valid document), not a 503 (a retry renders the same string through the same parser
      and fails the same way — the code would be a lie the UI would act on). Sentry sees it, with
      no locals. **Do not "fix" this row into a 503.**
    """
    if isinstance(exc, TailoringRunNotExportable):
        # X-4 / X-14. 409, not 422: the request was well-formed and named a run the caller owns —
        # it is the run's *state* that conflicts, exactly `tailoring_run_not_editable`'s reasoning
        # one aggregate over. `status` rides in the body so the client tells "still working, keep
        # polling" apart from "there is nothing to download" without a second read.
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "tailoring_run_not_exportable",
                "message": "This run has no documents to export yet.",
                "status": exc.status.value,
            },
        )

    if isinstance(exc, TooManyExportJobs):
        # X-18. **No number in the body**, unlike `too_many_tailoring_runs`: the cap is an operational
        # bound on a runaway loop, not a budget the visitor is meant to plan against, and a number on
        # the wire is a number the UI will eventually render as a quota.
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "too_many_export_jobs",
                "message": "You have reached the maximum number of exports for this session.",
            },
        )

    if isinstance(exc, ExportFormatNotQueued | ExportFormatNotInline):
        # X-15 / X-1 — the use cases' own second lock behind each endpoint's literal query/body type.
        # Reaching this branch means a caller got past the boundary's type, which today is possible
        # only from a non-HTTP caller; the answer is the **same** generic `validation_error` FastAPI
        # itself produces for the first lock, so the two locks are indistinguishable on the wire and
        # neither the code nor a message names the format the caller sent (AC-10's "the honest client
        # never sends one" — the sentence naming the exports endpoint was struck on 2026-09-17).
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "validation_error",
                "message": "The request could not be validated.",
            },
        )

    if isinstance(exc, ExportJobNotFound):
        # X-43. The same 404 for "no such id" and "not yours" — the use case collapses the two into
        # one type for exactly this, because an export job id is both the polling handle and the
        # download handle, so it is the id worth enumerating and a 403 would say when a guess landed.
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "code": "export_job_not_found",
                "message": "No export was found with that id.",
            },
        )

    if isinstance(exc, ExportNotReady):
        # X-44 / X-45. `status` always, `failure_reason` only when it failed — and the key is absent
        # rather than `null` on a `queued`/`rendering` job, because `ExportNotReady` carries `None`
        # for every status but `failed` and a present-but-null key would invite the client to read it.
        detail: dict[str, str] = {
            "code": "export_not_ready",
            "message": "That export is not ready to download.",
            "status": exc.status.value,
        }
        if exc.failure_reason is not None:
            detail["failure_reason"] = exc.failure_reason.value
        return HTTPException(status.HTTP_409_CONFLICT, detail=detail)

    if isinstance(exc, ExportNotQueued):
        # X-22, and the twin of `TailoringNotQueued` above with the money removed. 503 and not 500:
        # nothing about the request was wrong and trying again later is the right advice. By the time
        # this is raised the job row is committed and re-recorded `failed`/`not_queued` by the
        # router's second transaction, so the client is not left polling a job that can never run.
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "queue_unavailable",
                "message": "We couldn't start your export. Please try again.",
            },
        )

    if isinstance(exc, DocumentRenderTimedOut):
        # X-6 — **above** the `DocumentRenderFailed` floor, because it IS one. "It is taking too
        # long" and "it will not render" are different facts with different next actions, and this
        # is the one of the two where a retry can genuinely work.
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "render_timed_out",
                "message": "Preparing that document took too long. Please try again.",
            },
        )

    if isinstance(exc, DocumentRenderFailed):
        # X-5 — see this function's docstring. The one deliberate 500.
        return HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "render_failed",
                "message": "We couldn't prepare that document in that format.",
            },
        )

    # Unreachable through the union at the call site, and re-raised rather than smoothed into a
    # plausible 4xx for the reason `domain_error_to_http_exception`'s own floor gives: a type this
    # mapping does not know about is a bug, and a real 500 is the honest answer to it.
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
