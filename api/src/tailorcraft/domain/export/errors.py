"""Errors the `export` bounded context raises when a rule about an export breaks.

As in `intake`, `posting` and `tailoring`: an error class *is* its contract, so every one of these
gets a real body here rather than a `NotImplementedError` stub, even though this module lands in the
SKELETON step. There is nothing to defer — an error the RED test has to *construct* must be
constructible, and an error that carries data must actually store it, or the red fails on a
`TypeError` from the skeleton rather than on the assertion it was written for.

The domain never raises `HTTPException` and never carries a status code; translating one of these
into a response is the API layer's job (the failure contract's rows X-1 … X-47 in the feature spec
say which status and which `code` each becomes, and what each must log).

**Nothing here carries text.** Not a CV, not a cover letter, not Markdown, not HTML, not rendered
bytes, not a filesystem path, and above all not a renderer's own message — `weasyprint` logs CSS
parse warnings *with the offending declaration*, and the declaration comes from a document written
out of somebody's employment history. Ids, enums, counts and fixed labels only (Constitution §8).
"""

from __future__ import annotations

from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
)
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.value_objects import TailoringRunStatus


class ExportJobNotFound(DomainError):
    """No `ExportJob` exists with the requested id."""


class ExportJobNotOwnedBySession(DomainError):
    """An `ExportJob` exists, but for a different session than the one asking.

    The API layer maps this to the same 404 as `ExportJobNotFound` (ADR-0008): a guest session id is
    not authority over an object that references it, and answering "wrong session" instead of "not
    found" tells an attacker that the id they guessed is real. It stays a **distinct type the API
    never distinguishes**, for the reason `TailoringRunNotOwnedBySession`,
    `BaseCvNotOwnedBySession` and `JobPostingNotOwnedBySession` do: the use case's own tests need to
    tell "absent" from "not yours" apart to prove the ownership check exists at all, and a single
    type would let that check be deleted without a single test going red.
    """


class TooManyExportJobs(DomainError):
    """The session already owns `max_export_jobs_per_session` export jobs (X-18).

    A use-case check, not an invariant of `ExportJob` — the rule spans every job a session owns,
    which is a fact one aggregate has no way to know, and reaching for it from inside a constructor
    would mean a repository call in a constructor. **Soft**, exactly as `TooManyBaseCvs` (1.1's
    F-23) and `TooManyTailoringRuns` (1.3's G-10) are: two concurrent requests can both pass the
    count and overshoot by one, and that overshoot is accepted rather than locked. What this cap
    bounds is a runaway loop and the volume it would fill, not an exact number of rows.

    Carries nothing, the `TooManyBaseCvs` shape rather than the `TooManyTailoringRuns` one, because
    the limit is the same for every session and the router already knows it from `Settings`.
    """


class TailoringRunNotExportable(DomainError):
    """The run this export would render is not `succeeded`, so it has no current documents (X-4,
    X-14).

    Carries the status the run was actually in, so the router can give the two honest answers apart
    — "still working, keep polling" (`queued` / `running`) and "there is nothing to download"
    (`failed`) — without re-reading the aggregate. The check reads the run's **status** rather than
    `current_documents is not None`: 1.3's TR-2 makes the two equivalent, and the status is the one
    that says what it *means* (`BaseCvNotReadyForTailoring` carries the same argument).
    """

    def __init__(self, status: TailoringRunStatus) -> None:
        super().__init__(f"a tailoring run that is {status.value} cannot be exported")
        self.status = status


class ExportFormatNotQueued(DomainError):
    """A format that renders inline (`md` or `txt`) was handed somewhere only a queued format
    belongs: `ExportJob.request` (XJ-2, X-15) or `FileRef.for_export` (AC-7).

    An inline export leaves no row, no worker and no file, so a job for one would be an aggregate
    whose whole lifecycle is unreachable, and a `FileRef` for one would name a file that is never
    written. Refusing here is the **second** of three locks on the same rule — the `POST` body's
    `QueuedExportFormat` literal is the first and the `CHECK (format IN ('pdf','docx'))` is the
    third. Three, because the failure this rule prevents is WeasyPrint on the event loop, which
    passes every test with one user and collapses at five.

    Carries the format it was given, so the 422's body can name it without the router re-reading
    the request.
    """

    def __init__(self, format: ExportFormat) -> None:
        super().__init__(f"{format.value} is not a queued export format")
        self.format = format


class ExportFormatNotInline(DomainError):
    """A format that renders through a job (`pdf` or `docx`) was handed to `RenderDocumentInline`
    (X-1).

    The mirror of `ExportFormatNotQueued`, and a separate type rather than one
    `WrongExportDelivery(format, expected)` because the two point in opposite directions and are
    raised in two different layers of defence. This one is the second lock behind the download
    endpoint's `InlineExportFormat` query-parameter type; running WeasyPrint here is the exact
    failure ADR-0005 exists to prevent.
    """

    def __init__(self, format: ExportFormat) -> None:
        super().__init__(f"{format.value} is not an inline export format")
        self.format = format


class ExportNotReady(DomainError):
    """The job's file was asked for before there is one: the job is `queued`, `rendering` or
    `failed` (X-44, X-45).

    Carries **both** the status and the failure reason, the second of which is `None` for every
    status but `failed`. The pair is what lets the 409 body distinguish *keep polling* from *this
    one failed, and here is why* in one response, rather than making the client fetch the job
    resource again to find out which of the two it is — the same argument `TailoringAlreadyRunning`
    makes for carrying the active run's id: an error that only says "no" leaves the browser with
    nothing to do but ask again.
    """

    def __init__(self, status: ExportJobStatus, failure_reason: ExportFailureReason | None) -> None:
        super().__init__(f"an export job that is {status.value} has no file")
        self.status = status
        self.failure_reason = failure_reason


class ExportAlreadyStarted(DomainError):
    """`mark_started` was called on a job that is already `rendering`.

    The redelivery case, not a bug in the caller: a message the broker restores after its worker's
    main process was lost can find its own job already started. Refusing here is what makes
    **sequential** idempotency a property of the *aggregate* rather than a flag in a task (AC-18),
    exactly as `TailoringAlreadyStarted` does for a run. Two deliveries genuinely in flight at once
    both read `queued` and both pass this check; what stops the second is the version bump and
    `ExportJobConcurrentlyModified` on its `save` (AC-6, X-30) — 1.3 left that residual open and
    ADR-0015 §3 closed it, so this slice starts with it closed.
    """


class ExportNotRendering(DomainError):
    """`mark_ready` was called on a job that never started.

    A job goes `queued → rendering → ready`; there is no path from `queued` straight to a file,
    because a file comes from a render and `rendering` is what records that a render began.
    `mark_failed` is deliberately *not* subject to this rule — it is legal from `queued` too,
    because the enqueue can fail after the row is committed (X-22), and that failure must be
    recordable without pretending the job ever started.
    """


class ExportAlreadyDecided(DomainError):
    """A transition was called on a job whose outcome is already recorded — `ready` or `failed`.

    The outcome is decided **once** (XJ-4). A second decision, from a redelivery or a race, must not
    overwrite the first: a `ready` job's file is already sitting under a key the row names, and
    re-deciding it would leave the row and the bytes describing two different renders.
    `TailoringAlreadyDecided` and `ExtractionAlreadyDecided` guard the same shape in their contexts.

    There is deliberately no `retry()` and no `rerender()` anywhere near this. "Export again"
    creates a **new job**, at whatever the run's version is by then — which is also what makes
    `run_version` mean something (XJ-9): a job is never re-pointed at a new version.
    """


class ExportJobConcurrentlyModified(DomainError):
    """The row for this job changed between loading the aggregate and saving it: another process
    committed a newer `version` first (X-30, ADR-0015 §3 applied to a second aggregate).

    **Raised by the repository, never by the aggregate.** A reader looking for the `raise` in
    `export_job.py` will not find one, and that is the point of this docstring: the aggregate cannot
    see another process. It bumps `_version` on every transition (XJ-6) and that is the whole of its
    contribution; the imperative mapping declares the column as `version_id_col` with
    `version_id_generator=False`, SQLAlchemy emits `UPDATE … WHERE id = :id AND version = :loaded`,
    and on zero rows it raises its own `StaleDataError`, which the repository translates into this.
    Domain-defined and adapter-raised, exactly as `ExportNotQueued` is raised by the queue adapter.

    **This is what makes two simultaneous deliveries of one job render once** (AC-6): both read
    `queued`, both pass `ExportAlreadyStarted`, and the loser's `save` after `mark_started` raises
    this — `RenderExportJob` then returns `SKIPPED` *before* rendering, so the renderer is called
    once. Carries the job's id, which the repository reads into a local **before** the flush: a
    failed flush expires the whole identity map inside the flush, so reading `job.id` afterwards is
    a lazy load on an `AsyncSession` and a `MissingGreenlet` (1.4's lesson, CLAUDE.md).
    """

    def __init__(self, job_id: ExportJobId) -> None:
        super().__init__(f"export job {job_id.value} was modified concurrently")
        self.job_id = job_id


class InvalidRunVersion(DomainError):
    """`ExportJob.request` was given a `run_version` below 1 (XJ-8, AC-2).

    A `TailoringRun` is at version 1 the moment it is requested (ADR-0015) and only ever counts up,
    so 0 or a negative number is not a stale value — it is a caller that did not read the run, and
    the job it would create could never be `current` for anything. Refused at construction rather
    than checked later, because the comparison `job.run_version == run.version` is what every other
    rule in this slice hangs off.
    """


class ExportNotQueued(DomainError):
    """The broker refused to accept the render task: Redis is down, or `send_task` failed (X-22).

    Raised by `CeleryExportQueue`, and — the part worth stating — a **plain `DomainError`, not a
    `DocumentRenderFailed`**. 1.3 made the same call about `TailoringNotQueued` and the argument
    carries over with the money removed: nothing was rendered. The renderer was never reached, no
    worker second was spent and no bytes exist, so this is not an outcome of a render and does not
    belong in the taxonomy of ways a render can fail. What follows it is `mark_failed(NOT_QUEUED)`
    recorded by the router on a job that committed and can now never run — more honest than leaving
    it `queued` for ever while a client polls it until it gives up.

    Carries nothing. The broker's own message can quote the broker URL, password included.
    """


class DocumentRenderFailed(DomainError):
    """Base for every way `DocumentRendererPort.render` can fail.

    Carries the `ExportFailureReason` the use case needs to call `ExportJob.mark_failed` and the
    adapter needs for a stable `failure_reason` in its log line — the one thing this exception
    exists to communicate. The same shape `TailoringFailed`, `CvExtractionFailed` and
    `JobPostingFetchFailed` have.

    **`render` raises one of these subclasses on every failure**, and that promise is kept
    structurally — by an `except Exception` floor in the adapter, never by an allow-list of the
    library errors we happened to think of. CLAUDE.md records losing exactly that bet in the
    extraction sweep, and there are now four libraries behind this one port. `Exception`, not
    `BaseException`: `asyncio.CancelledError` must still cancel (X-36).

    **Caught, not propagated, on the queued path** — `RenderExportJob` converts it into
    `mark_failed(reason)` on a committed row, because by then the user is waiting on a poll and
    there is an artifact to own (ADR-0014 §2). On the **inline** path there is no row and nothing
    was spent, so `RenderDocumentInline` lets `DocumentRenderError` propagate to a 500 (X-5). One
    exception family, two deliberately different fates, decided by whether anything exists to
    record the failure on.
    """

    def __init__(self, reason: ExportFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class DocumentRenderFailedOnDocument(DocumentRenderFailed):
    """The pipeline reached a stage it has a name for and that stage refused the document: a parse
    that produced nothing usable, a walker that could not map a token stream, a renderer that
    returned no bytes.

    Named for the *document* rather than for a library, because which of the four libraries was
    holding it when it gave up is the adapter's business and never the domain's — and because the
    honest user-facing answer ("that document could not be prepared in this format") is the same
    either way. `DocumentRenderError` below is the residual for everything this does not cover.
    """

    def __init__(self) -> None:
        super().__init__(ExportFailureReason.RENDER_FAILED)


class DocumentRenderTimedOut(DocumentRenderFailed):
    """The render ran past its per-format timeout under `asyncio.wait_for`.

    A distinct reason from `RENDER_FAILED` because "it is taking too long" and "it will not render"
    are different facts with different next actions: this one is worth retrying and its *rate* is
    the signal that a document size or a stylesheet change has moved the cost. `wait_for` cancels
    the await; the thread running the synchronous library is the hard time limit's problem, which
    is why the stale window must sit above it (the second startup guard, D3).
    """

    def __init__(self) -> None:
        super().__init__(ExportFailureReason.RENDER_TIMED_OUT)


class DocumentRenderOutputTooLarge(DocumentRenderFailed):
    """The rendered bytes exceeded `export_max_file_bytes`.

    Refused rather than truncated and never written: half a PDF on the uploads volume is a file the
    user can download and cannot open, and one whose size nothing bounds is a volume somebody else's
    document was going to need. Checked on the **produced bytes**, which is the only number that is
    a fact rather than a claim.
    """

    def __init__(self) -> None:
        super().__init__(ExportFailureReason.OUTPUT_TOO_LARGE)


class DocumentRenderError(DocumentRenderFailed):
    """The residual: the adapter's `except Exception` floor reached for something it has no better
    name for (X-37).

    Deliberately the one subclass with no specific cause, the same role `LLM_ERROR`,
    `FETCHER_ERROR` and `EXTRACTOR_ERROR` play in their contexts. A named subclass per unknown cause
    would be a taxonomy of things we specifically failed to identify, and the allow-list version of
    that idea is the bet the extraction sweep lost. This class is what makes the port's promise true
    by construction: the floor goes underneath, the specific translations sit on top carrying the
    better reason, and the adapter logs the exception's **type** only and re-raises `from None`
    (AC-34) — the frame it came from holds the Markdown, the HTML and the bytes, and `sentry_sdk`
    defaults `include_local_variables=True`.
    """

    def __init__(self) -> None:
        super().__init__(ExportFailureReason.RENDER_ERROR)
