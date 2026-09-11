"""The `RequestTailoringRun` use case: accept a request to tailor a base CV to a job posting, and
record it as a `queued` run for a worker to pick up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tailorcraft.application.intake.get_base_cv import GetBaseCvForSession
from tailorcraft.application.posting.get_job_posting import GetJobPostingForSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId, BaseCvStatus
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.errors import (
    BaseCvNotReadyForTailoring,
    TailoringAlreadyRunning,
    TooManyTailoringRuns,
)
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus


@dataclass(frozen=True, slots=True)
class RequestTailoringRunCommand:
    """What the caller supplies: who is asking, and which two things to combine.

    Three ids and nothing else — no text, no bytes, no prompt. Both inputs already exist and are
    already owned by a session; this use case's whole job is to check that and to record the
    intent. The three ids are typed value objects rather than bare `UUID`s for the reason
    `TailoringRun`'s TR-1 gives: this command holds three UUIDs of three different things, and
    transposing two of them would produce a run tailoring the wrong person's CV with no error
    anywhere.
    """

    guest_session_id: GuestSessionId
    base_cv_id: BaseCvId
    job_posting_id: JobPostingId


@dataclass(frozen=True, slots=True)
class RequestTailoringRunResult:
    """What the caller gets back: enough to start polling, and nothing more.

    `status` is always `QUEUED` at this point — it is returned rather than assumed so the router
    serializes the aggregate's own answer instead of a literal, which is what keeps the polling
    client and the aggregate speaking one vocabulary. There is deliberately no `documents` field
    and no `metrics` field: nothing has run yet, and a result type that *could* carry them would
    invite a caller to check.
    """

    tailoring_run_id: TailoringRunId
    status: TailoringRunStatus
    requested_at: datetime


class RequestTailoringRun:
    """Record a visitor's request to tailor one base CV against one job posting.

    **It takes two *use cases*, not two repositories, and that is the load-bearing choice here.**
    `GetBaseCvForSession` and `GetJobPostingForSession` each carry an authorization rule — *what
    authorizes access is the link*, `cv.guest_session_id == the resolved session id`, checked on
    every read, because owning a session id is not authority over an object that references it
    (ADR-0008). Slice 1.2's plan said in as many words that 1.3 and 1.5 would reach a `JobPosting`
    from a second entry point and that *a check centralized there is a check they get for free*.
    **This is that entry point**, and this is the promise being collected.

    **The cost, stated rather than hidden:** each of the two composed use cases resolves the session
    itself, so requesting a run performs **two extra primary-key lookups of the guest-session row**
    — two `SELECT`s on a PK index, on a row already in Postgres' cache, once per request. What that
    buys is that the "not mine → 404, never 403" rule cannot be forgotten in a third place. The
    alternative — `cvs.get(id)` and `postings.get(id)` straight from here, plus a hand-written
    ownership comparison — saves the two lookups and re-implements two rules that already exist,
    which is exactly how the third copy of a check comes to differ from the first two.

    **Why there is no `queue.enqueue(...)` line in this use case.** A reader will look for one, so:
    enqueuing is deliberately *outside* here, in the router, **after the commit** (ADR-0014 §5). The
    ordering is not a preference between two workable options — the other order is broken. The
    worker is a separate process on the same Redis and pickup latency is measured in milliseconds,
    so *enqueue-then-commit* would routinely hand a task a run id whose row is not committed yet:
    every such task finds nothing, returns `MISSING`, and the run sits `queued` forever. That is a
    **race that fires under normal load**, not a crash window. *Commit-then-enqueue* leaves a crash
    between two statements whose survivor is a `queued` run with no task — visible in the database,
    visible to the user as "waiting", recoverable by re-enqueuing. ADR-0006 §2's rule applied
    unchanged: choose the crash window whose survivor is recoverable. The commit boundary is the
    router's (`get_session` opens one `AsyncSession` per request and the handler commits inside its
    own error boundary), and a use case that cannot see that boundary must not straddle it.

    Flow (technical-plan.md, "Application layer"):

    1. ``cv = await get_base_cv(cmd.base_cv_id, cmd.guest_session_id)`` — resolves the session and
       raises `GuestSessionNotFound` / `GuestSessionExpired` / `BaseCvNotFound` (G-5, G-6).
    2. ``posting = await get_job_posting(cmd.job_posting_id, cmd.guest_session_id)`` —
       `JobPostingNotFound` (G-7).
    3. ``if cv.status is not BaseCvStatus.EXTRACTED: raise BaseCvNotReadyForTailoring(...)`` (G-8).
       The check reads the **aggregate's own status**, not `extracted_text is not None`: I-2 makes
       the two equivalent, and the status is the one that says what it *means*.
    4. ``if await runs.find_active_for_session(sid) is not None: raise TailoringAlreadyRunning(id)``
       (G-9). Cross-aggregate, therefore here. The error carries the active run's id so the router
       can hand the client something to attach to instead of paying for a second call.
    5. ``if await runs.count_for_session(sid) >= max_per_session: raise TooManyTailoringRuns(...)``
       (G-10).
    6. ``run_id = runs.next_identity()``; ``run = TailoringRun.request(...)``; ``await runs.add(run)``.
    7. ``await events.publish(*run.release_events())`` — after the aggregate is saved, never before.
    8. Return `RequestTailoringRunResult` built from the saved aggregate.

    **Both caps at steps 4 and 5 are cross-aggregate policy and both are soft**, and neither belongs
    on `TailoringRun`:

    - *Why not on the aggregate.* Each rule spans **every run a session owns** — the set of a
      session's runs is a fact no single `TailoringRun` instance has access to. Reaching for it from
      inside `request` would mean a repository call in a constructor, which is the road to a domain
      layer that cannot be tested without a database (ADR-0014 §4). `TooManyBaseCvs` (1.1) and
      `TooManyJobPostings` (1.2) live in their use cases for the identical reason.
    - *Why soft.* Two genuinely concurrent requests may both pass either check and both create a
      run. That is **accepted**, exactly as 1.1's F-23 and 1.2's P-32 accepted the same shape. The
      alternative is a unique partial index or a lock, which buys correctness against a double-click
      that the disabled button already prevents, at the cost of a lock on the hot path. The rule's
      job is to stop a double-click from buying two paid calls and to make "reattach after a
      refresh" trivial — it was never a mutual exclusion.

    **No row exists before the enqueue on any rejection path** (ADR-0014 §2, AC/OQ-2). Every raise
    above happens before step 6, so a 401, a 404, a 409 or a 429 creates nothing to own and nothing
    to purge: *was anything spent, and is there an artifact to own?* is the one question, and before
    the enqueue the answer is no.
    """

    def __init__(
        self,
        runs: TailoringRunRepository,
        get_base_cv: GetBaseCvForSession,
        get_job_posting: GetJobPostingForSession,
        events: EventPublisherPort,
        clock: Clock,
        max_per_session: int = 20,
    ) -> None:
        self._runs = runs
        self._get_base_cv = get_base_cv
        self._get_job_posting = get_job_posting
        self._events = events
        self._clock = clock
        self._max_per_session = max_per_session

    async def __call__(self, cmd: RequestTailoringRunCommand) -> RequestTailoringRunResult:
        # Both reads go through the composed *use cases* rather than the two repositories, so the
        # "what authorizes access is the link" rule (ADR-0008) is inherited rather than written a
        # third time. Each resolves the guest session itself — that is the pair of extra
        # primary-key lookups this class's docstring puts a price on, paid deliberately.
        cv = await self._get_base_cv(cmd.base_cv_id, cmd.guest_session_id)
        posting = await self._get_job_posting(cmd.job_posting_id, cmd.guest_session_id)

        # G-8 reads the **aggregate's own status**, not `cv.extracted_text is not None`. I-2 makes
        # the two equivalent, so this is not a correctness choice — it is a meaning one: the status
        # is the attribute that says *why* the CV cannot be tailored, and it is what the error
        # carries to the caller. A `None` check would also leave a reader wondering which of the two
        # is authoritative the first time they disagree.
        if cv.status is not BaseCvStatus.EXTRACTED:
            raise BaseCvNotReadyForTailoring(cv.id, cv.status)

        # The next two checks are **cross-aggregate policy, and neither belongs on `TailoringRun`**:
        # each spans every run the session owns, which is a fact no single aggregate instance has
        # any way to know. Reaching for it from inside `TailoringRun.request` would mean a
        # repository call in a constructor — the road to a domain layer that cannot be tested
        # without a database (ADR-0014 §4). `TooManyBaseCvs` (1.1) and `TooManyJobPostings` (1.2)
        # live in their use cases for the identical reason.
        #
        # Both are also **soft**. Two genuinely concurrent requests can pass either check and both
        # create a run; that is accepted, exactly as 1.1's F-23 and 1.2's P-32 accept the same
        # shape. The alternative is a unique partial index or a lock on the hot path, bought to beat
        # a double-click that the disabled button already prevents.
        #
        # Their **order is load-bearing**: the active-run rule is checked first, so a visitor who
        # already has a run in flight is told *that*, and handed its id to attach a poller to,
        # rather than being told they are at the cap — which would also be true, and useless.
        active_run = await self._runs.find_active_for_session(cmd.guest_session_id)
        if active_run is not None:
            raise TailoringAlreadyRunning(active_run.id)

        run_count = await self._runs.count_for_session(cmd.guest_session_id)
        if run_count >= self._max_per_session:
            raise TooManyTailoringRuns(run_count, self._max_per_session)

        # Identity is application-assigned (ADR-0007): the aggregate is valid before it ever meets
        # the database. The two ids come off the aggregates just loaded rather than off `cmd` —
        # same values, but these two are the ones ownership was actually checked on.
        run = TailoringRun.request(
            id=self._runs.next_identity(),
            guest_session_id=cmd.guest_session_id,
            base_cv_id=cv.id,
            job_posting_id=posting.id,
            requested_at=self._clock.now(),
        )
        await self._runs.add(run)

        # Released and published only after the aggregate is saved — never before, so a publish
        # never announces a fact a failed save is about to un-happen. The enqueue that follows is
        # the router's, after the commit; see this class's docstring for why it is not on this line.
        await self._events.publish(*run.release_events())

        return RequestTailoringRunResult(
            tailoring_run_id=run.id,
            status=run.status,
            requested_at=run.requested_at,
        )
