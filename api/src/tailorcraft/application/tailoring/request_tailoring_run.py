"""The `RequestTailoringRun` use case: accept a request to tailor a base CV to a job posting, and
record it as a `queued` run for a worker to pick up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from tailorcraft.application.billing.charge_credit_for_run import (
    ChargeCreditCommand,
    ChargeCreditForRun,
)
from tailorcraft.application.intake.get_base_cv import GetBaseCv
from tailorcraft.application.posting.get_job_posting import GetJobPosting
from tailorcraft.domain.billing.value_objects import ChargedRunRef
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.intake.value_objects import BaseCvId, BaseCvStatus
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.errors import (
    AccountRequired,
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

    # Who the run will belong to — a creating command keeps the word "owner" (2.2's
    # `UploadBaseCvCommand` precedent). Both inputs are authorized against this same owner, and the
    # run takes it, so the ownership graph never crosses owners (ADR-0022).
    owner: Owner
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
    `GetBaseCv` and `GetJobPosting` each carry an authorization rule — *what
    authorizes access is the link*, `cv.owner == the resolved requester`, checked on
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

    Flow (slice 5.2, plan §0.3; a `GuestOwner` is refused `AccountRequired` before step 1):

    1. ``get_base_cv`` / ``get_job_posting`` (404s, ownership); ``EXTRACTED`` (409, G-8).
    2. The **soft** active-run check (G-9), then the per-user cap (G-10). Both before any lock.
    3. ``run = TailoringRun.request(...)``; ``await runs.add(run)``.
    4. ``await charge(...)``: takes the account lock, writes the pending charge (`NoCreditsLeft`,
       `RunAlreadyCharged`).
    5. The **hard** re-check, ``find_active_for_owner(owner, excluding=run.id)``, under that lock.
    6. Publish the run's events, then the charge's.

    **The soft checks are soft; the re-check is not.** Two concurrent requests can both pass step
    2, as 1.1's F-23 accepted. Before credits that bought a double-click two paid calls; now it would
    also charge twice, so step 5 closes it: the second request waits on the first's account lock and
    then sees the first's committed run (F-2). The rule stays here, in tailoring; billing never sees it.

    **Every refusal after step 3 rolls back with the handler's session**, so the run and the charge
    vanish together (F-1): nothing commits until the handler does.

    **No row survives a rejection** (ADR-0014 §2, AC/OQ-2). Every raise before step 3 writes
    nothing, and every one after it is rolled back, so a 401, a 404, a 409 or a 429 creates nothing to own and nothing
    to purge: *was anything spent, and is there an artifact to own?* is the one question, and before
    the enqueue the answer is no.
    """

    def __init__(
        self,
        runs: TailoringRunRepository,
        get_base_cv: GetBaseCv,
        get_job_posting: GetJobPosting,
        events: EventPublisherPort,
        clock: Clock,
        max_per_session: int = 20,
        max_per_user: int = 500,
        *,
        charge: ChargeCreditForRun,
    ) -> None:
        # `max_per_session` is unused since guests stopped requesting runs (plan §2); it goes when
        # the composition root stops passing it.
        self._charge = charge
        self._runs = runs
        self._get_base_cv = get_base_cv
        self._get_job_posting = get_job_posting
        self._events = events
        self._clock = clock
        self._max_per_user = max_per_user

    async def __call__(self, cmd: RequestTailoringRunCommand) -> RequestTailoringRunResult:
        # Tailoring costs a credit and a credit is a user's (slice 5.2). The guest route no longer
        # reaches here, so this arm is the last line of defence, kept so the `match` stays total;
        # it raises before anything is read, so a guest request writes and locks nothing.
        match cmd.owner:
            case GuestOwner():
                raise AccountRequired()
            case UserOwner(user_id=user_id):
                pass
            case _:
                assert_never(cmd.owner)

        # Both reads go through the composed *use cases* rather than the two repositories, so the
        # "what authorizes access is the link" rule (ADR-0008) is inherited rather than written a
        # third time.
        cv = await self._get_base_cv(cmd.base_cv_id, cmd.owner)
        posting = await self._get_job_posting(cmd.job_posting_id, cmd.owner)

        # G-8 reads the **aggregate's own status**, not `cv.extracted_text is not None`: I-2 makes
        # the two equivalent, and the status is the attribute that says *why*.
        if cv.status is not BaseCvStatus.EXTRACTED:
            raise BaseCvNotReadyForTailoring(cv.id, cv.status)

        # The soft checks: cross-aggregate policy, so here and not on `TailoringRun`. The active-run
        # rule goes first so a visitor with a run in flight is handed its id rather than told they
        # are at the cap. Both run before the lock, so a refusal here never waits on it.
        active_run = await self._runs.find_active_for_owner(cmd.owner)
        if active_run is not None:
            raise TailoringAlreadyRunning(active_run.id)

        run_count = await self._runs.count_for_owner(cmd.owner)
        if run_count >= self._max_per_user:
            raise TooManyTailoringRuns(run_count, self._max_per_user)

        # Plan §0.3 step 4: the run is inserted **before** the account lock is taken, so the lock
        # is held for as little as possible and a request waiting on it finds this run on waking.
        now = self._clock.now()
        run = TailoringRun.request(
            id=self._runs.next_identity(),
            owner=cmd.owner,
            base_cv_id=cv.id,
            job_posting_id=posting.id,
            requested_at=now,
        )
        await self._runs.add(run)

        # Step 5: one credit, under the account lock held until the handler's transaction ends.
        # `NoCreditsLeft` / `RunAlreadyCharged` propagate; the handler's rollback takes the run too.
        charge_events = await self._charge(
            ChargeCreditCommand(user_id, ChargedRunRef(value=run.id.value), now)
        )

        # Step 6, the **hard** re-check (F-2): still under the lock, so a rival request that held it
        # first has committed its run by now and READ COMMITTED's per-statement snapshot sees it.
        # The soft check above could not. `excluding` keeps this request from refusing itself.
        rival = await self._runs.find_active_for_owner(cmd.owner, excluding=run.id)
        if rival is not None:
            raise TailoringAlreadyRunning(rival.id)

        # Published only after both writes, the run's events first, then the account's.
        await self._events.publish(*run.release_events(), *charge_events)

        return RequestTailoringRunResult(
            tailoring_run_id=run.id,
            status=run.status,
            requested_at=run.requested_at,
        )
