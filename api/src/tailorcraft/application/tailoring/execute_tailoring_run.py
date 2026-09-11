"""The `ExecuteTailoringRun` use case: the worker's half of a tailoring run — pick up a queued run,
call the model, and record what happened.

This is a **T11 skeleton** (docs/sdlc.md §2): the signatures below are final, `__call__` raises
`NotImplementedError`, and T13 fills it in against the reds `qa` records at T12.

Read this module's class docstring next to `request_tailoring_run.RequestTailoringRun`'s. The two
are deliberately asymmetric in how they reach a `BaseCv` and a `JobPosting`, and each says why by
pointing at the other; either one read alone looks like an inconsistency worth "fixing".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.posting.ports import JobPostingRepository
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.ports import LlmPort, TailoringRunRepository
from tailorcraft.domain.tailoring.value_objects import TailoringRunId

# **This layer does not log, and that is the convention rather than an omission.** No use case in
# `application/` has a logger — across two shipped slices, not one — and this slice keeps it that
# way. The application layer's channel for saying what happened is `events.publish(...)`:
# `LoggingEventPublisher` logs every field of every event it receives, generically and with no
# per-event allow-list, so publishing `TailoringRunFailed` *is* the log line, and it is the one
# AC-21's privacy test captures.
#
# The reason is sharper than tidiness. AC-21 asserts that no fragment of a CV, a posting or a
# tailored document appears in captured **structlog** output across a full successful run and a full
# failed one. A second logging channel opened here — the stdlib's `logging`, say, on the argument
# that `structlog` is a third party this layer may not import — would emit records that test does not
# necessarily see, and a privacy test that cannot see a channel passes vacuously for everything sent
# down it. Two logging paths where the guard covers one is exactly the shape of the 1.2 `httpx`
# finding, one layer up.
#
# So where a finer-grained label is genuinely needed — `problem=cv_text_missing` at step 5's
# impossible branch — it is recorded on the aggregate as a failure reason and emitted by the task
# (`infrastructure/tasks/tailoring.py`), which is infrastructure and has `structlog`.


@dataclass(frozen=True, slots=True)
class ExecuteTailoringRunCommand:
    """The run to execute, and nothing else.

    One field, on purpose (ADR-0014 §5, §6). The id is the only argument the queued task ever
    receives, and everything else this use case needs it reads back from the row — which is what
    makes the task idempotent by construction rather than by a flag: there is no second copy of the
    inputs travelling through Redis that could disagree with the database, and no payload that a
    redelivery could replay against a different row.
    """

    tailoring_run_id: TailoringRunId


class ExecuteTailoringRunOutcome(StrEnum):
    """What the worker did — the task's whole return vocabulary.

    Five values rather than a `bool` or a raised exception, because the Celery task above this use
    case is a **thin entry point** whose only job is to translate an outcome into a log line
    (ADR-0005). Four of these five are perfectly ordinary and none of them is an error the task
    should retry: `task_acks_late = True` makes redelivery real, and a redelivery of a run that is
    already decided, already running, gone, or abandoned is an expected event, not a fault.

    A `StrEnum` so the value logs as `outcome=skipped` rather than as
    `ExecuteTailoringRunOutcome.SKIPPED`, matching `TailoringRunStatus` and
    `TailoringFailureReason` next door.

    Deliberately **not** the same enum as `TailoringRunStatus`: this describes what *this invocation*
    did, and the run's status describes where the *run* stands. `SKIPPED` and `MISSING` have no
    status counterpart at all, and a `FAILED` outcome and a `failed` status are only incidentally
    spelled alike — the outcome is the worker's report, the status is the recorded fact.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"  # already decided, or already running and not yet stale
    MISSING = "missing"  # the run is gone (its session was purged)
    ABANDONED = "abandoned"  # redelivered after the stale window


class ExecuteTailoringRun:
    """Execute one queued `TailoringRun`: mark it started, call the model, and record the outcome —
    success or failure — as a **state of the aggregate**, never as an exception that escapes.

    **The contrast with `RequestTailoringRun`, stated as a contrast because it is the design.** That
    use case composes `GetBaseCvForSession` and `GetJobPostingForSession` — two *use cases*, each
    carrying an authorization rule — precisely so the "not mine → 404" check cannot be forgotten at
    a new entry point. This one takes `BaseCvRepository` and `JobPostingRepository` — two
    **repositories**, used directly, with no ownership check anywhere in it — and that is deliberate
    rather than the check having been dropped on the way to the worker.

    The reason is that the two use cases are answering different questions. `RequestTailoringRun` is
    acting *on behalf of a caller who might not own these rows*, and must find out. This one is not
    acting on behalf of anybody: **the run it loads already encodes the authorization decision**,
    made and committed at request time by `RequestTailoringRun`, against a session that was live
    then. `run.base_cv_id` and `run.job_posting_id` are not ids a stranger supplied — they are ids
    this system wrote into a row after checking them.

    Re-running the ownership check here would also be actively wrong, not merely redundant. It
    resolves the session, and a guest session can expire (or be purged, ADR-0006) between the
    request and the pickup — a twelve-second call queued behind a backlog is enough. The check would
    then raise `GuestSessionExpired` and fail a run **for a reason that has nothing to do with
    tailoring**, on work the user is already waiting for. The authorization question was asked at
    the only moment it had a meaningful answer.

    Flow (technical-plan.md, "Application layer"; T13 implements it):

    1. ``run = await runs.find(cmd.tailoring_run_id)``; ``None`` → return `MISSING` (G-26). No
       exception and no retry — `find` returns `None` rather than raising exactly so absence is an
       ordinary branch in the worker (see `TailoringRunRepository.find`). One log line at the task.
    2. ``run.status`` terminal → return `SKIPPED` (G-27). **No LLM call is made** — this is AC-10,
       and it is the aggregate's TR-3 doing the work: a redelivery cannot buy a second call.
    3. ``run.status is RUNNING``: if ``now - run.started_at > stale_after_seconds``, then
       ``run.mark_failed(ABANDONED, now)``, save, publish, return `ABANDONED` (G-25); otherwise
       return `SKIPPED`. The stale window is what tells "a worker is mid-call right now" apart from
       "a worker died holding this run and nobody is coming back for it".
    4. ``run.mark_started(clock.now())``; ``await runs.save(run)`` **and commit**.
    5. ``cv = await base_cvs.get(run.base_cv_id)``; ``posting = await job_postings.get(...)``.
    6. ``draft = await llm.tailor(cv.extracted_text, posting.text)``, with `TailoringFailed`
       **caught**.
    7. ``run.mark_succeeded(draft.documents, draft.metrics, clock.now())``; save; publish; return
       `SUCCEEDED`.

    Three of those steps carry a decision that is invisible from the code alone, so each is written
    out here as well as commented at its own line in T13.

    **Step 4 — two commits, and this is the only place in the codebase that needs them.** The save
    that records `RUNNING` is committed **on its own**, in a separate transaction from the one that
    records the outcome. Not tidiness: a tailoring call takes on the order of twelve seconds, and
    the client is polling `GET /api/tailoring-runs/{id}` throughout. If `running` were written
    inside the same transaction as the outcome, the polling client would see `queued` for the whole
    call and then jump straight to `succeeded` — indistinguishable, from the outside, from a run
    that never got picked up, which is precisely the "still working" vs. "this failed" distinction
    the frontend is required to make. **This is why the worker's unit of work is explicit rather
    than one request-scoped session**: `deps.get_session`'s session-per-request shape has exactly
    one commit in it, so the worker gets its own composition root (`infrastructure/tasks/`), and
    `TailoringRunRepository.save` deliberately says nothing about transactions so that the boundary
    stays the caller's.

    **Step 6 — `TailoringFailed` is caught here, and that is the deliberate opposite of
    `CaptureJobPosting`**, which lets `JobPostingFetchFailed` propagate to the boundary and records
    nothing (ADR-0013). Catch it, ``run.mark_failed(exc.reason, clock.now())``, save, publish,
    return `FAILED`. The line that decides which of the two shapes applies is ADR-0014 §2 — *was
    anything spent, and is there an artifact to own?* Past the enqueue the answer is yes on both
    counts: Google has been paid on the user's behalf and twelve seconds of somebody's afternoon
    have gone, so the run owns the fact that it happened and the reason it produced nothing. A
    failed run is a **recorded state**, never a 500 with nothing on disk (ADR-0004). Note for
    whoever is tempted to simplify this into a propagating error: **an application test asserts the
    recording, not the propagation** (T12, G-16…G-23), so that change turns a test red rather than
    passing quietly — which is the whole point of writing it that way.

    **Step 5 — the near-unreachable branch.** A `BaseCvNotFound` or `JobPostingNotFound` here means
    the guest session was purged between step 1 and now; but the purge cascades, so the run row went
    with it, and the branch is therefore effectively unreachable. It is handled as `MISSING` — log
    one line, return, do not raise — rather than as a failure state, **because there is no run left
    to record a failure on**. T13 carries one line of comment saying exactly that, so the branch
    does not read as a forgotten failure reason someone should fill in.

    **The narrowing at step 6.** `cv.extracted_text` is `ExtractedText | None` on the aggregate, and
    `mypy --strict` will insist on the check even though step 3 of `RequestTailoringRun` already
    guaranteed the status is `EXTRACTED` and I-2 makes that equivalent to the text being present. If
    it *is* `None` here, the CV changed underneath us — a genuine impossibility rather than a
    contingency — so record `TailoringFailureReason.LLM_ERROR` and log `problem=cv_text_missing`.
    Explicitly **not** `INPUTS_TOO_LARGE`: nothing was measured and nothing was too large, and a
    reason chosen because it was nearby is a reason that lies to whoever reads the failure
    breakdown. One branch, one comment.

    **What this use case does not do.** It declares no retry and needs none: retrying is a decision
    that requires knowing what kind of failure occurred, and only the adapter has that (ADR-0014
    §6). Two retry layers do not add, they multiply.
    """

    def __init__(
        self,
        runs: TailoringRunRepository,
        base_cvs: BaseCvRepository,
        job_postings: JobPostingRepository,
        llm: LlmPort,
        events: EventPublisherPort,
        clock: Clock,
        stale_after_seconds: int = 300,
    ) -> None:
        self._runs = runs
        self._base_cvs = base_cvs
        self._job_postings = job_postings
        self._llm = llm
        self._events = events
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds

    async def __call__(self, cmd: ExecuteTailoringRunCommand) -> ExecuteTailoringRunOutcome:
        raise NotImplementedError
