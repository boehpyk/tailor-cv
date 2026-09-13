"""The `ExecuteTailoringRun` use case: the worker's half of a tailoring run — pick up a queued run,
call the model, and record what happened.

Read this module's class docstring next to `request_tailoring_run.RequestTailoringRun`'s. The two
are deliberately asymmetric in how they reach a `BaseCv` and a `JobPosting`, and each says why by
pointing at the other; either one read alone looks like an inconsistency worth "fixing".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.posting.ports import JobPostingRepository
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.errors import TailoringFailed
from tailorcraft.domain.tailoring.ports import LlmPort, TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)

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
        # Step 1. `find`, not `get`: absence is an ordinary branch for the worker, not an exception
        # (see `TailoringRunRepository.find`). A guest session purged at the 24-hour mark (ADR-0006)
        # cascades its runs away while a message for one of them is still sitting in Redis, and
        # `task_acks_late=True` makes redelivery of an already-purged id real rather than
        # theoretical. Raising here would hand a routine outcome to the worker's error machinery.
        run = await self._runs.find(cmd.tailoring_run_id)
        if run is None:
            return ExecuteTailoringRunOutcome.MISSING

        # Step 2. A decided run is done, for good (TR-3). Returning before the LLM is what makes a
        # redelivery unable to buy a second paid call — AC-10 — and the aggregate would refuse
        # `mark_started` anyway; this branch is here so the refusal is a return value rather than an
        # exception the task would have to interpret.
        if run.status in (TailoringRunStatus.SUCCEEDED, TailoringRunStatus.FAILED):
            return ExecuteTailoringRunOutcome.SKIPPED

        # Step 3. A run that is already `RUNNING` has two very different explanations, and the stale
        # window is the only thing that tells them apart: a worker is mid-call right now (leave it
        # alone), or a worker died holding it and nobody is coming back (record it and stop the
        # client polling forever).
        if run.status is TailoringRunStatus.RUNNING:
            now = self._clock.now()
            started_at = run.started_at
            stale_after = timedelta(seconds=self._stale_after_seconds)
            # `started_at` cannot be `None` while the status is `RUNNING` — `mark_started` sets both
            # in one breath and nothing else writes either — but `mypy --strict` cannot see that, so
            # the narrowing is a real branch. It is folded into the stale side deliberately: a run
            # that claims to be running and cannot say since when is precisely "nobody is coming
            # back for it", and the alternative (treat it as fresh) would leave it `RUNNING` for
            # ever, which is the one outcome this window exists to prevent.
            if started_at is None or now - started_at > stale_after:
                await self._record_failure(run, TailoringFailureReason.ABANDONED, now)
                return ExecuteTailoringRunOutcome.ABANDONED
            return ExecuteTailoringRunOutcome.SKIPPED

        # Step 4. **The first of two transactions**, and the only place in the codebase that needs a
        # second one. `running` has to be durable *before* the call, not with its outcome: the call
        # takes on the order of twelve seconds and the client polls throughout, so a single
        # transaction would show `queued` for the whole call and then jump to a terminal status —
        # indistinguishable from a run nobody ever picked up, which is exactly the "still working"
        # vs. "this failed" distinction the frontend owes the user. Committing is the caller's job
        # (`TailoringRunRepository.save` says nothing about transactions on purpose); the worker's
        # composition root closes this one here. An application test snapshots `save_calls` at the
        # instant `tailor()` starts, so moving this below the call turns it red.
        run.mark_started(self._clock.now())
        await self._runs.save(run)
        # Published after the save that made it durable, never before — and released per
        # transaction, so `TailoringRunStarted` goes out with the state it describes rather than
        # arriving at the end bundled with the outcome.
        await self._events.publish(*run.release_events())

        # Step 5. Both reads go straight to the repositories, with no ownership check — see this
        # class's docstring: the run already encodes the authorization decision made at request
        # time, and re-asking against a session that may have expired since would fail the run for a
        # reason that has nothing to do with tailoring.
        try:
            cv = await self._base_cvs.get(run.base_cv_id)
            posting = await self._job_postings.get(run.job_posting_id)
        except (BaseCvNotFound, JobPostingNotFound):
            # Effectively unreachable: the only way an input disappears is the guest-session purge,
            # and that cascades the run row away with it, so step 1 would have returned `MISSING`
            # already. Handled as `MISSING` rather than as a failure reason **because there is no
            # run left to record a failure on** — this is not a forgotten `TailoringFailureReason`.
            return ExecuteTailoringRunOutcome.MISSING

        cv_text = cv.extracted_text
        if cv_text is None:
            # A genuine impossibility, not a contingency: `RequestTailoringRun` refused anything but
            # an `EXTRACTED` CV, and I-2 makes that equivalent to the text being present. So the CV
            # changed underneath us. Recorded as `LLM_ERROR` — explicitly **not**
            # `INPUTS_TOO_LARGE`, which is right next door and would fit the signature: nothing was
            # measured and nothing was too large, and a reason picked because it was nearby lies to
            # whoever reads the failure breakdown later. The finer label (`problem=cv_text_missing`)
            # belongs to the task, which has a logger; this layer has none, by the convention at the
            # top of this module. No call to the model is made on this path.
            await self._record_failure(run, TailoringFailureReason.LLM_ERROR, self._clock.now())
            return ExecuteTailoringRunOutcome.FAILED

        # Step 6. **`TailoringFailed` is caught here, and that is the deliberate opposite of
        # `CaptureJobPosting`**, which lets `JobPostingFetchFailed` propagate to the boundary and
        # records nothing (ADR-0013). The line deciding which of the two shapes applies is ADR-0014
        # §2 — *was anything spent, and is there an artifact to own?* Before the enqueue the answer
        # is no on both counts, so 1.2 records nothing; past the enqueue it is yes on both: Google
        # has been paid on the user's behalf and twelve seconds of somebody's afternoon are gone, so
        # the run owns the fact that it happened and the reason it produced nothing. A failed run is
        # a recorded state, never a 500 with nothing on disk (ADR-0004).
        #
        # The base class, not a tuple of its seven subclasses: `TailoringFailed` is what carries
        # `.reason`, and an allow-list of subclasses would be the same bet-you-enumerated-them-all
        # mistake `LlmPort.tailor`'s docstring warns about — the one the extractor sweep lost.
        #
        # For whoever is tempted to simplify this into a propagating error: the application tests
        # assert the **recording** — the outcome, the run's status, its `failure_reason` — not the
        # propagation, so that change turns them red rather than passing quietly.
        try:
            draft = await self._llm.tailor(cv_text, posting.text)
        except TailoringFailed as exc:
            await self._record_failure(run, exc.reason, self._clock.now())
            return ExecuteTailoringRunOutcome.FAILED

        # Step 7. The second transaction. `clock.now()` is read again rather than reused from step 4
        # — the whole point of two timestamps is that the gap between them is the call.
        run.mark_succeeded(draft.documents, draft.metrics, self._clock.now())
        await self._runs.save(run)
        await self._events.publish(*run.release_events())
        return ExecuteTailoringRunOutcome.SUCCEEDED

    async def _record_failure(
        self, run: TailoringRun, reason: TailoringFailureReason, at: datetime
    ) -> None:
        """Record a decided-as-failed run: `mark_failed`, save, then publish — in that order.

        The three failure paths above (`ABANDONED`, `cv_text_missing`, and every `TailoringFailed`)
        differ only in the reason and in the instant, so the save/publish ordering is written once.
        Publishing strictly **after** the save is the rule `RequestTailoringRun` step 7 states: a
        publish that ran first would announce a fact a failed save is about to un-happen.
        `AbandonStaleTailoringRuns` performs the same three steps per run, in the same order, and
        its docstring says why the two do not share a helper.

        The five metric scalars are deliberately not touched. `mark_failed` already guarantees they
        stay `None` on every path, and a partially-filled metrics row would show up in a token-spend
        total as a real number nothing was paid for.
        """
        run.mark_failed(reason, at)
        await self._runs.save(run)
        await self._events.publish(*run.release_events())
