"""Errors the `tailoring` bounded context raises when a rule about a tailoring run breaks.

Unlike the value objects, these carry no behaviour to defer to a later step — an error class *is* its
contract, so every one of them gets a real body here rather than a `NotImplementedError` stub,
exactly as `domain/intake/errors.py` and `domain/posting/errors.py` do. The domain never raises
`HTTPException` and never carries a status code; translating one of these into a response is the API
layer's job (see `docs/specs/tailoring-generate-documents/technical-plan.md` for the status/`code`
mapping, and the failure contract rows G-1 … G-34 in the feature spec for what each one must log).

**Nothing here carries text.** Not a CV, not a posting, not a prompt, not a completion, not a
provider's message. An exception is caught, logged and re-raised by code that has no idea what is in
its message, and `sentry_sdk` defaults `include_local_variables=True` on top of that. Ids, enums,
counts and fixed labels only (Constitution §8).
"""

from __future__ import annotations

from tailorcraft.domain.intake.value_objects import BaseCvId, BaseCvStatus
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason, TailoringRunId


class TailoringRunNotFound(DomainError):
    """No `TailoringRun` exists with the requested id."""


class TailoringRunNotOwnedBySession(DomainError):
    """A `TailoringRun` exists, but for a different session than the one asking.

    The API layer maps this to the same 404 as `TailoringRunNotFound` (G-29, ADR-0008): a guest
    session id is not authority over an object that references it, and answering "wrong session"
    instead of "not found" tells an attacker that the id they guessed is real. The distinction stays
    two error types rather than one, though, because the use case's own tests need to tell "absent"
    from "not mine" apart even when the boundary must not — collapsing them here would leave nothing
    able to prove the ownership check runs at all. `BaseCvNotOwnedBySession` and
    `JobPostingNotOwnedBySession` exist for the same reason, and `GetTailoringRunForSession` raises
    the 404 `from` this one so a test can read it off `__cause__` (AC-14).
    """


class TooManyTailoringRuns(DomainError):
    """The session already owns the maximum number of tailoring runs (G-10).

    Carries the count it saw and the limit it compared against, so the router can tell the user what
    the limit is rather than only that they hit one.

    A use-case check, not an invariant of `TailoringRun` — the rule spans every run a session owns,
    which is a fact one aggregate has no way to know, and reaching for it from inside the aggregate
    would mean a repository call in a constructor. **Soft**: two concurrent requests can both pass it
    and overshoot by one, and that overshoot is accepted rather than locked, exactly as
    `TooManyBaseCvs` (1.1's F-23) and `TooManyJobPostings` (1.2's P-32) accept it. A cap whose job is
    to stop a runaway loop does not need to be exact; a lock that made it exact would serialize every
    request in the session.
    """

    def __init__(self, count: int, limit: int) -> None:
        super().__init__(f"session already owns {count} tailoring runs; the limit is {limit}")
        self.count = count
        self.limit = limit


class TailoringAlreadyRunning(DomainError):
    """The session already has a run in flight — `queued` or `running` — and may not start a second
    one (G-9, ADR-0014 §4).

    Carries the **active run's id**, and that payload is the whole point rather than a convenience:
    the router puts it in the 409 body so the client can attach its poller to the run that is already
    paying for itself instead of the user double-clicking their way to two Gemini calls. An error
    that only said "no" would leave the browser with nothing to do but ask again.

    Cross-aggregate, therefore in the use case and not on `TailoringRun`, and **soft** for the same
    reason `TooManyTailoringRuns` is: two genuinely simultaneous requests may both find no active run
    and both proceed. The rate limiter bounds the hour; this bounds the double-click; neither is a
    lock and neither pretends to be.
    """

    def __init__(self, active_run_id: TailoringRunId) -> None:
        super().__init__(f"session already has an active tailoring run: {active_run_id.value}")
        self.active_run_id = active_run_id


class BaseCvNotReadyForTailoring(DomainError):
    """The base CV this run would tailor has no usable text yet (G-8).

    Carries the CV's id and the status it was actually in, so the router can distinguish the two
    honest answers — "extraction has not finished" (`UPLOADED`) and "extraction failed, upload
    another file" (`EXTRACTION_FAILED`) — without re-reading the aggregate.

    The check reads the aggregate's **status**, not `extracted_text is not None`. Invariant I-2 makes
    the two equivalent, and the status is the one that says what it *means*: a reader of
    `if cv.status is not BaseCvStatus.EXTRACTED` learns the business rule, while a reader of the
    `None` check learns an implementation detail and has to go and find out whether it implies the
    rule.
    """

    def __init__(self, base_cv_id: BaseCvId, status: BaseCvStatus) -> None:
        super().__init__(f"base CV {base_cv_id.value} is {status.value}, not extracted")
        self.base_cv_id = base_cv_id
        self.status = status


class TailoringAlreadyStarted(DomainError):
    """`mark_started` was called on a run that is already `running`.

    The redelivery case, not a bug in the caller: Celery runs with `task_acks_late`, so a task whose
    worker died mid-call is redelivered and the second attempt finds its own run already started.
    Refusing here is what makes idempotency a property of the *aggregate* rather than a flag in a
    task (TR-3, AC-10).
    """


class TailoringNotRunning(DomainError):
    """`mark_succeeded` was called on a run that never started.

    A run goes `queued → running → succeeded`; there is no path from `queued` straight to two
    documents, because documents come from a call and a call is what `running` records. `mark_failed`
    is deliberately *not* subject to this rule — it is legal from `queued` too, because the enqueue
    can fail after the row is committed (G-14) and a redelivered task can find an abandoned run
    (G-25), and both must be recordable without pretending the run ever started.
    """


class TailoringAlreadyDecided(DomainError):
    """A transition was called on a run whose outcome is already recorded — `succeeded` or `failed`.

    The outcome is decided **once** (TR-3). A second decision, from a retry or a race, must not
    silently overwrite the first: the user has already been shown one answer, and the money for it
    has already been spent. `ExtractionAlreadyDecided` guards the same shape in `intake`.

    There is deliberately no `retry()` and no `rerun()` anywhere near this. "Try again" creates a new
    run, so that what a run cost and what a run produced stay one immutable fact (TR-6).
    """


class EmptyTailoredDocument(DomainError):
    """A tailored document is blank or whitespace-only — the model returned nothing usable."""


class TailoredDocumentTooShort(DomainError):
    """A tailored document is below its floor: 400 non-whitespace characters for a `TailoredCv`, 200
    for a `CoverLetter`.

    A model that answers "Here is your tailored CV:" and stops has failed, and the value object is
    where that is decided rather than a check every consumer has to remember. The floors are
    **chosen, not measured** (OQ-5) — verify them against the eval corpus and change them once, on
    purpose, in the spec and the code together.
    """


class TailoredDocumentTooLong(DomainError):
    """A tailored document is above its ceiling: 20,000 characters for a `TailoredCv`, 8,000 for a
    `CoverLetter`.

    Raised, never silently truncated — a document cut in half without telling anyone is a wrong
    answer the user cannot diagnose, and here they paid for it. The ceiling bounds what one call can
    push into Postgres and into 1.5's renderer, and a 30,000-character "cover letter" is a runaway
    generation rather than content.
    """


class InvalidTailoredDocument(DomainError):
    """A tailored document carries a control character other than `\\n` or `\\t`, or a NUL.

    NUL is the one worth naming: it is a C-side string terminator, so a document carrying one can
    mean two different things to two different readers of the same bytes — the database, the
    renderer and the browser need not agree about where it ends.

    Note what this error is **not** about: HTML. `<script>` in a tailored CV is valid content as far
    as this type is concerned, because rejecting `<` would reject "C++ → C# migration" and sanitizing
    would be a rendering policy hiding in a domain type. Sanitizing belongs to whoever renders the
    text into HTML — see `TailoredCv`'s docstring.
    """


class InvalidModelName(DomainError):
    """`ModelName` was given something that cannot identify a model: blank, longer than 64
    characters, or carrying whitespace or a control character."""


class InvalidPromptVersion(DomainError):
    """`PromptVersion` was given something outside its grammar: blank, longer than 16 characters, or
    carrying a character outside `[A-Za-z0-9._-]`. The grammar is closed because this value is a key
    — a persisted column, a log field and any future regression comparison all read it."""


class InvalidLlmCallMetrics(DomainError):
    """`LlmCallMetrics` was given a negative token count or a negative duration.

    Zero is legal for all three: a provider that reports no usage metadata is a gap in observability,
    not a reason to fail a run that produced two good documents. A negative one is a bug in the
    adapter's arithmetic, and it should surface where it was made rather than as an impossible number
    in a latency percentile three weeks later.
    """


class TailoringFailed(DomainError):
    """Base for every way `LlmPort.tailor` can fail.

    Carries the `TailoringFailureReason` the use case needs to call `TailoringRun.mark_failed` and
    the adapter needs for a stable `failure_reason` in its log line — the one thing this exception
    exists to communicate. The same shape `CvExtractionFailed` and `JobPostingFetchFailed` have.

    **`tailor` raises one of these subclasses on every failure**, and that promise is kept
    structurally — by an `except Exception` floor in the adapter, never by an allow-list of the SDK
    errors we happened to think of. CLAUDE.md records losing exactly that bet in the extraction
    sweep, and ADR-0012's obligation 10 records it again. `Exception`, not `BaseException`:
    `asyncio.CancelledError` must still cancel (G-24).

    **The behaviour on the way out is 1.1's, not 1.2's**, and the difference matters enough to name
    here. `JobPostingFetchFailed` propagates through its use case to the router, because a failed
    fetch has no artifact to own. This one is **caught** by `ExecuteTailoringRun` and converted into
    a recorded state of the aggregate (`mark_failed`), because by the time it can be raised the run
    is a committed row, the user is waiting on a poll, and money may already have been spent — ADR-
    0014 §2's line, *was anything spent, and is there an artifact to own?*, answers yes on both
    counts. An application test asserts the *recording* rather than the propagation, so "fixing" this
    into a propagating error turns a test red rather than quietly changing behaviour.
    """

    def __init__(self, reason: TailoringFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class LlmUnavailable(TailoringFailed):
    """The provider could not be reached or answered 5xx: DNS, a refused connection, a failed
    handshake, an outage. Retryable — the adapter retries once before giving up, and the client is
    offered "Try again" (AC-13)."""

    def __init__(self) -> None:
        super().__init__(TailoringFailureReason.LLM_UNAVAILABLE)


class LlmRateLimited(TailoringFailed):
    """The provider answered 429: our quota, not the user's mistake.

    A separate reason from `LLM_UNAVAILABLE` because the two suggest different next actions and
    because a rise in this one alone is a billing signal worth alerting on — it is the difference
    between "Google is down" and "we outgrew our tier".
    """

    def __init__(self) -> None:
        super().__init__(TailoringFailureReason.LLM_RATE_LIMITED)


class LlmRefused(TailoringFailed):
    """The model declined to answer — a safety block, a blocked prompt, an empty candidate list with
    a refusal `finish_reason`.

    **Never retried.** A refusal retried is a refusal repeated at twice the price: the inputs are
    identical and the decision was the model's, so a second call is a second payment for the same
    answer. This is also why the client offers no "Try again" for this reason (AC-13) — the honest
    message is that this pairing of CV and posting will not tailor, not that the machine hiccuped.
    """

    def __init__(self) -> None:
        super().__init__(TailoringFailureReason.LLM_REFUSED)


class LlmTimedOut(TailoringFailed):
    """The call ran past its per-attempt timeout or the run past its total deadline.

    Distinct from `LLM_UNAVAILABLE` because "it is taking too long" and "it is not there" are
    different facts about the provider, and because the 15-second budget (Constitution §7) is defended
    by watching this reason's rate rather than by hoping.
    """

    def __init__(self) -> None:
        super().__init__(TailoringFailureReason.LLM_TIMED_OUT)


class LlmOutputInvalid(TailoringFailed):
    """The provider answered successfully and the answer was not usable: not JSON, not an object, a
    missing or wrongly-typed field, or a document outside its bounds.

    This is the reason ADR-0004's "structured output is re-validated on receipt" exists. *We asked
    the model for JSON* and *this is valid JSON with the fields we need* are different claims, and
    the gap between them is where the 2 a.m. bug lives — a schema in the request is a request, not a
    guarantee.

    **`problem` is a fixed label and never the offending text.** The offending text is half a CV, and
    an exception message travels into every log line and every Sentry frame that touches it. The
    labels themselves (`not_json`, `not_object`, `missing_field`, `wrong_type`,
    `document_too_short`, `document_too_long`, `document_invalid`, …) live with the parser in
    `infrastructure/llm/parsing.py` rather than here: they are a parser's vocabulary for *how a
    response was malformed*, which is an infrastructure concern, and the domain's vocabulary stops at
    "the output was invalid". The field is typed `str` for that reason — an enum here would drag the
    parser's taxonomy into the domain and would have to grow every time a new malformation is
    recognised.
    """

    def __init__(self, problem: str) -> None:
        super().__init__(TailoringFailureReason.LLM_OUTPUT_INVALID)
        self.problem = problem


class LlmInputsTooLarge(TailoringFailed):
    """The CV and the posting together exceed what one call may carry, refused **before any API
    call** (G-22).

    Rejected rather than truncated, and never retried — nothing about a second attempt would be
    different, and a truncated CV would produce a tailored document missing the half of someone's
    career we silently dropped. The bound is the adapter's configuration, which is why this reason is
    raised by the adapter and not enforced by a value object: it moves when the model does.
    """

    def __init__(self) -> None:
        super().__init__(TailoringFailureReason.INPUTS_TOO_LARGE)


class LlmError(TailoringFailed):
    """The residual: the adapter's `except Exception` floor reached for something it has no better
    name for.

    Deliberately the one subclass with no specific cause, the same role `FETCHER_ERROR` and
    `EXTRACTOR_ERROR` play in their contexts. A named subclass per unknown cause would be a taxonomy
    of things we specifically failed to identify — and the allow-list-of-known-exceptions version of
    that bet is precisely what CLAUDE.md records losing in the extraction sweep, where `KeyError`,
    `AttributeError`, `ValueError` and `LimitReachedError` all escaped a port that promised to
    translate every failure.

    This is also what `ExecuteTailoringRun` records for the genuine impossibility — a base CV whose
    `extracted_text` is `None` at execution time, when the request path had already proved it was
    not — logged with `problem=cv_text_missing` rather than dressed up as an input-size failure.
    """

    def __init__(self) -> None:
        super().__init__(TailoringFailureReason.LLM_ERROR)


class TailoringNotQueued(DomainError):
    """`TailoringQueuePort.enqueue` could not hand the run to the broker — Redis is unreachable, or
    `kombu` refused the publish (G-14).

    **A `DomainError` and deliberately not a `TailoringFailed`**, and the distinction is ADR-0014
    §2's line applied to the one moment it is hardest to see: *was anything spent?* No. No call was
    made, no tokens were bought, no model was asked anything. `TailoringFailed` is the vocabulary of
    a call that happened; this is the vocabulary of a call that never will.

    The row is a different question from the exception. By the time this is raised the run has
    already been committed as `queued` (commit-then-enqueue, ADR-0014 §5), so the router catches this
    and records the run `failed` with `TailoringFailureReason.NOT_QUEUED` before answering 503 —
    leaving it `queued` forever would be a run the client polls until it gives up. That is why
    `NOT_QUEUED` is a reason with no exception subclass: it is written by our orchestration, not
    raised by a port.
    """
