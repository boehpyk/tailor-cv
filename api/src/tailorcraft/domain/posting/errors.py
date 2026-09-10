"""Errors the `posting` bounded context raises when a rule about a captured job description breaks.

Unlike the value objects, these carry no behaviour to defer to a later step — an error class *is*
its contract, so it gets a real body here rather than a `NotImplementedError` stub, exactly as
`domain/intake/errors.py` does. The domain never raises `HTTPException` and never carries a status
code; translating one of these into a response is the API layer's job (see
`docs/specs/posting-job-description-intake/technical-plan.md` for the status/`code` mapping, and the
failure contract rows P-4 … P-10 in the feature spec for what each one must log).
"""

from __future__ import annotations

from tailorcraft.domain.posting.value_objects import FetchFailureReason
from tailorcraft.domain.shared.errors import DomainError


class JobPostingNotFound(DomainError):
    """No `JobPosting` exists with the requested id."""


class JobPostingNotOwnedBySession(DomainError):
    """A `JobPosting` exists, but for a different session than the one asking.

    The API layer maps this to the same 404 as `JobPostingNotFound` (ADR-0008): a guest session id
    is not authority over an object that references it, and answering "wrong session" instead of
    "not found" tells an attacker that the id they guessed is real. The distinction stays two error
    types rather than one, though, because the use case's own tests need to tell "absent" from "not
    mine" apart even when the boundary must not — collapsing them here would leave nothing able to
    prove the ownership check runs at all. `BaseCvNotOwnedBySession` exists for the same reason.
    """


class InvalidSourceUrl(DomainError):
    """`SourceUrl` was given something that is not a fetchable job-posting URL: a scheme outside
    `{http, https}`, no hostname, userinfo, more than 2,048 characters, or a control character,
    NUL or whitespace anywhere in it (P-8, P-9, P-10).

    All five rules collapse to one error type on purpose. The boundary answers 422
    `invalid_source_url` for every one of them, and a per-rule taxonomy would be a set of types
    nothing ever discriminates on — while the *log* line differs per row (P-9 logs nothing about the
    URL at all, because the string contains a credential), and that is the router's decision to
    make, not this type's.
    """


class InvalidPostingTitle(DomainError):
    """`PostingTitle` was given something that cannot be a display label: empty after `strip()`,
    longer than 200 characters, or carrying a control character or NUL. The title comes off a page a
    stranger wrote, so it is validated like untrusted input rather than trusted for having been in a
    `<title>` tag."""


class EmptyJobPostingText(DomainError):
    """The job description is blank or whitespace-only (P-4)."""


class JobPostingTextTooShort(DomainError):
    """The job description has fewer than 100 non-whitespace characters after normalization (P-5).

    Not enough for a model to tailor against, so it is refused at the value-object boundary rather
    than admitted as a partial success every consumer would have to remember to check. The floor is
    100 rather than `ExtractedText`'s 200 because a two-paragraph posting is a real document —
    see `JobPostingText` for the full reasoning and OQ-5 for its provisional status.
    """


class JobPostingTextTooLong(DomainError):
    """The job description exceeds 30,000 non-whitespace characters after normalization (P-6).

    Raised, never silently truncated: trimming would change the user's input without telling them,
    and a tailoring run against text they did not know was cut is a wrong answer they cannot
    diagnose. The ceiling bounds 1.3's token budget and what one fetch of a stranger's URL can push
    into Postgres and into a prompt.
    """


class TooManyJobPostings(DomainError):
    """The session already owns the maximum number of job postings.

    This is a use-case check, not an invariant of `JobPosting` — the rule spans every `JobPosting` a
    session owns, which is a fact a single aggregate has no way to know, and reaching for one from
    inside the aggregate would mean a repository call in a constructor. A soft cap: concurrent
    requests can overshoot it slightly, and that overshoot is accepted rather than locked, exactly
    as `TooManyBaseCvs` accepts it (OQ-10 in slice 1.1).
    """


class JobPostingFetchFailed(DomainError):
    """Base for every way `JobPostingFetcherPort.fetch` can fail.

    Carries the `FetchFailureReason` the router needs to pick a status and a `code`, and the adapter
    needs to write a stable `failure_reason` into its log line — the one thing this exception exists
    to communicate.

    **The deliberate contrast with `CvExtractionFailed`, which is the whole of ADR-0013:** that one
    is *caught* by `UploadBaseCv` and converted into a recorded state of the aggregate
    (`BaseCv.mark_extraction_failed`), because a failed extraction still leaves bytes on a volume
    that a row must account for and a purge must find. **This one is not caught. It propagates
    straight through the use case to the router**, which turns it into an HTTP error with a stable
    `code`, and the product's answer is FR-2's paste fallback.

    The reason is that a failed fetch has no artifact: no bytes, no text, no file, nothing on disk.
    A row would record an *event*, not an artifact, and this codebase has domain events for that —
    except that there is no aggregate to record one on either, so the failure is a log line and a
    response and nothing else. Keeping it that way is what gives 1.3 the invariant that makes it
    simple: **a `JobPosting` always has usable text**, so the tailoring use case never asks whether
    a posting's text is really there. Admitting a textless `JobPosting` would push a `None` check
    into every consumer forever.

    A reader arriving from slice 1.1 will expect the recorded-state shape and should find this
    paragraph instead of an inconsistency to "fix". The application test asserts the propagation, so
    converting it into a recorded state turns a test red rather than quietly changing behaviour.
    """

    def __init__(self, reason: FetchFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class SourceUrlNotAllowed(JobPostingFetchFailed):
    """The URL's target is one this process refuses to open a socket to: the host resolved to a
    loopback, private, link-local, CGNAT, multicast, reserved or unspecified address — including the
    cloud metadata endpoint at `169.254.169.254`, which is the thing SSRF is *for* (ADR-0012
    obligations 2-3). Raised **before any connection is made**, and re-raised at any redirect hop
    whose target fails the same check."""

    def __init__(self) -> None:
        super().__init__(FetchFailureReason.BLOCKED_TARGET)


class SourceUnreachable(JobPostingFetchFailed):
    """The host could not be reached at all: DNS did not resolve, the connection was refused, or the
    TLS handshake failed. The user's URL may simply be wrong — this is not evidence of a bug
    here."""

    def __init__(self) -> None:
        super().__init__(FetchFailureReason.UNREACHABLE)


class SourceTimedOut(JobPostingFetchFailed):
    """The fetch ran past its configured timeout. A separate reason from `UNREACHABLE` because the
    two suggest different next actions to the user — "check the address" versus "try again" — and
    because a rise in timeouts alone is a signal worth alerting on."""

    def __init__(self) -> None:
        super().__init__(FetchFailureReason.TIMED_OUT)


class SourceRejectedRequest(JobPostingFetchFailed):
    """The host answered, and its answer was no: a 4xx or 5xx status, most often the 403 a job board
    returns to anything that is not a browser. Distinct from `UNREACHABLE` precisely so the user can
    be told the site refused us rather than that it does not exist — the paste fallback is the
    answer either way, but only one of those messages is true."""

    def __init__(self) -> None:
        super().__init__(FetchFailureReason.REJECTED)


class SourceTooManyRedirects(JobPostingFetchFailed):
    """The chain exceeded the redirect budget. Redirects are followed manually, a hop at a time, so
    that the entire guard re-runs on each one; a bounded budget is what stops a redirect loop from
    becoming an unbounded amount of work done on a stranger's instruction."""

    def __init__(self) -> None:
        super().__init__(FetchFailureReason.TOO_MANY_REDIRECTS)


class SourceResponseTooLarge(JobPostingFetchFailed):
    """The response exceeded the byte cap, detected **while streaming** rather than after the fact —
    a cap enforced on `Content-Length` alone is a cap enforced on a number a hostile server
    chose."""

    def __init__(self) -> None:
        super().__init__(FetchFailureReason.RESPONSE_TOO_LARGE)


class SourceNotHtml(JobPostingFetchFailed):
    """The response was not an HTML document — a PDF, an image, a JSON API. There is no text
    extractor behind this port, and guessing at one would quietly turn a wrong URL into a posting
    made of binary noise."""

    def __init__(self) -> None:
        super().__init__(FetchFailureReason.NOT_HTML)


class SourceHasNoReadableText(JobPostingFetchFailed):
    """The page parsed but yielded no usable text — the fetched analogue of a scanned PDF with no
    text layer. Typically a client-rendered page whose body is a single empty `<div>`, which is a
    large and growing share of the job boards this feature will meet."""

    def __init__(self) -> None:
        super().__init__(FetchFailureReason.NO_READABLE_TEXT)


class SourceTextTooLong(JobPostingFetchFailed):
    """The page yielded text past `JobPostingText`'s ceiling. Raised by the adapter as a *fetch*
    failure rather than surfacing `JobPostingTextTooLong`, because from the user's side these are
    two different events with two different remedies: their own paste was too long, versus the page
    we fetched on their behalf was mostly navigation chrome. Same underlying bound, different story
    to tell — which is exactly the kind of distinction a shared error type would erase."""

    def __init__(self) -> None:
        super().__init__(FetchFailureReason.TEXT_TOO_LONG)
