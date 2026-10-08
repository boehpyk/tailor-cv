"""The `RequestExport` use case: accept a request to export one of a run's documents into one
**queued** format, and record it as a `queued` `ExportJob` for a worker to pick up.

Read this module's class docstring next to `render_export_job.RenderExportJob`'s. The two are
deliberately asymmetric in how they reach a `TailoringRun` — a read *use case* here, a *repository*
there — and each says why by pointing at the other; either one read alone looks like an
inconsistency worth "fixing". `request_tailoring_run` and `execute_tailoring_run` draw exactly the
same contrast one context over, and for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.export.errors import TailoringRunNotExportable, TooManyExportJobs
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.ports import ExportJobRepository
from tailorcraft.domain.export.value_objects import (
    DEFAULT_LAYOUT_TEMPLATE,
    ExportFormat,
    ExportJobStatus,
    LayoutTemplate,
)
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.value_objects import (
    TailoredDocumentKind,
    TailoringRunId,
    TailoringRunStatus,
)


@dataclass(frozen=True, slots=True)
class RequestExportCommand:
    """What the caller supplies: who is asking, which run, which document, and which format.

    No text, no bytes, no version. The document's Markdown is read back off the run by the worker,
    and the `run_version` the job records is read off the run *here* rather than taken from the
    caller — a client-supplied version would be a second copy of a fact the database already holds,
    free to disagree with it, and it would let a caller pin a job to a version that was never
    current.

    `requester` and `tailoring_run_id` are typed value objects rather than bare `UUID`s for
    `RequestTailoringRunCommand`'s reason: two UUIDs of two different things, and a transposition
    would otherwise be a silent lookup of the wrong row.

    `format` is an `ExportFormat` — the whole enum, not the queued subset — on purpose. The HTTP
    boundary narrows it to `pdf` / `docx` with a literal type (X-10), and `ExportJob.request`
    refuses an inline member with `ExportFormatNotQueued` (X-15). This command sits between two
    locks and adds no third: a subset type here would mean a second enum to keep in step with
    `ExportFormat`, and the aggregate's refusal is the one that cannot be routed around.
    """

    # Who is asking (slice 2.3, §0.3). Not called `owner`: the job's owner is taken from the *run*
    # (ADR-0016 amendment (a)), and this command only says who presented a credential.
    requester: Owner
    tailoring_run_id: TailoringRunId
    document: TailoredDocumentKind
    format: ExportFormat
    # Slice 3.2 (ADR-0016 amendment): the PDF layout the caller chose, `None` when it chose none.
    # Optional so every existing caller stays valid; the use case applies the default for a PDF
    # and the aggregate refuses a layout on a DOCX (`LayoutTemplateNotApplicable`).
    layout_template: LayoutTemplate | None = None


@dataclass(frozen=True, slots=True)
class RequestExportResult:
    """The job to poll, and whether this request is what created it.

    `created=False` means the **existing current job was returned** (X-16): the idempotent lookup
    found a `queued`, `rendering` or `ready` job for this exact (run, document, format) key,
    requested at the run's current version, and nothing new was written.

    The flag exists because the router must answer **202 on a create and 200 on a return**, and
    that is a distinction it cannot re-derive from the job alone — a `queued` job looks identical
    either way. It is a `bool` rather than a status enum because there are exactly two answers and
    neither will grow a third: a rejection does not reach this type at all, it raises.

    Note what is **not** here: no `file_url`, no `data`, no queue handle. Nothing has rendered, and
    a result type that could carry bytes would invite a caller to look for them.
    """

    export_job: ExportJob
    created: bool


class RequestExport:
    """Record a visitor's request to export one document of one run into one queued format, or hand
    back the job that is already doing exactly that.

    **It takes the read *use case* `GetTailoringRun`, not `TailoringRunRepository`, and
    that is the load-bearing choice here.** This is the fourth time this codebase makes it — 1.3
    made it twice (`GetBaseCv`, `GetJobPosting`) and 1.4 made it once
    (`ReviseTailoredDocument`) — so it is a convention now rather than a judgement call, but the
    reason is worth restating because it is the whole argument for the shape:

        The read use case carries the authorization rule — *what authorizes access is the link*,
        `run.owner == GuestOwner(the resolved session id)` (ADR-0008) — **and** the 404 collapse
        that makes "not mine" indistinguishable from "does not exist" (X-13, AC-14). A use case
        that never sees a run repository cannot forget either of them.

    1.3's plan predicted this entry point in as many words and said a check centralized there is a
    check this slice gets for free. This is the promise being collected, on the id an attacker is
    most likely to be enumerating: a run id is the polling handle *and* now the export handle.

    **The cost, stated rather than hidden**, exactly as `RequestTailoringRun` states it: the
    composed use case resolves the session itself, so requesting an export performs one extra
    primary-key lookup of the guest-session row — a `SELECT` on a PK index, on a row Postgres has
    cached, once per request. What it buys is that the "not mine → 404, never 403" rule cannot be
    written a fourth time and get it subtly wrong.

    **The use case does not enqueue**, and a reader will look for the line, so: publishing the task
    is the router's, **after the commit** (ADR-0014 §5). The ordering is not a preference between
    two workable options — the other order is broken. The worker sits on the same Redis and pickup
    latency is milliseconds, so *enqueue-then-commit* would routinely hand a task a job id whose
    row is not committed: the task finds nothing, returns `MISSING` (X-33), and the job sits
    `queued` for ever behind a spinner. *Commit-then-enqueue* leaves a crash window whose survivor
    is a `queued` job with no task — visible in the database, visible as "preparing", and
    recoverable. ADR-0006 §2's rule unchanged: choose the crash window whose survivor is
    recoverable. The commit boundary is the router's (`get_session` opens one `AsyncSession` per
    request), and a use case that cannot see that boundary must not straddle it.

    Flow (technical-plan.md, "Application layer" §1; T7 implements it):

    1. ``run = await get_tailoring_run(cmd.tailoring_run_id, cmd.requester)`` — resolves the
       session and raises `GuestSessionNotFound` / `GuestSessionExpired` (X-12) and
       `TailoringRunNotFound` (X-13), the latter for both "absent" and "not mine".
    2. ``if run.status is not TailoringRunStatus.SUCCEEDED: raise TailoringRunNotExportable(
       run.status)`` (X-14). Reads the **aggregate's own status**, not `run.current_documents is
       not None`: the two are equivalent, and the status is the one that says what it *means* and
       the one the error carries to the client.
    3. ``layout = cmd.layout_template``, or `DEFAULT_LAYOUT_TEMPLATE` when that is `None` and
       ``cmd.format.takes_layout_template`` (slice 3.2); ``existing = await
       jobs.find_latest_for_key(run.id, cmd.document, cmd.format, layout)``. If
       `existing` is not `None`, is not `failed`, and ``existing.was_requested_for(run.version)``:
       return ``RequestExportResult(existing, created=False)`` — 200, no row, no task (X-16).
    4. ``if await jobs.count_for_session(sid) >= max_per_session`` (guest) … ``: raise
       TooManyExportJobs(...)`` (X-18).
    5. ``job = ExportJob.request(id=jobs.next_identity(), ..., layout_template=layout,
       run_version=run.version, requested_at=clock.now())`` — `ExportFormatNotQueued` (X-15) and
       `LayoutTemplateNotApplicable` (a DOCX with a layout) propagate from here, and
       `InvalidRunVersion` cannot fire, because a run's version is 1 or more by construction.
    6. ``await jobs.add(job)``; ``await events.publish(*job.release_events())``; return
       ``RequestExportResult(job, created=True)``.

    **Step 3 is cross-aggregate and therefore here, not on `ExportJob`.** It compares a job to a
    *run* — `job.run_version` against `run.version` — and no `ExportJob` instance can see a run.
    `was_requested_for` is the aggregate's half of that comparison, which is as far as it can go.

    **Step 3 is also soft** (X-23). Two genuinely concurrent `POST`s for one key can both miss the
    lookup and both create a job, and that is accepted exactly as 1.1's F-23, 1.2's P-32 and 1.3's
    G-34 accepted the same shape. What the rule is for is stopping a second *click* from buying a
    second render; it was never a mutual exclusion, and the alternative — a unique partial index or
    a lock on the request path — buys correctness against a double-click that the disabled control
    already prevents.

    **Step 4's cap is cross-aggregate policy too, and also soft**, for the reason
    `TooManyTailoringRuns` and `TooManyBaseCvs` give: the rule spans **every job a session owns**,
    which is a fact no single `ExportJob` has access to, and reaching for it from inside `request`
    would mean a repository call in a constructor — the road to a domain layer that cannot be
    tested without a database (ADR-0014 §4).

    **The order of steps 3 and 4 is load-bearing.** The idempotent lookup runs *first*, so a
    visitor at the cap who clicks a control for a job that already exists is handed that job rather
    than a 409 telling them they have too many — which would also be true, and useless, and would
    make the cap turn a working repeat request into a failure.

    **No row exists on any rejection path** (ADR-0014 §2). Every raise above happens before step 5,
    so a 401, a 404, a 409 or a 422 creates nothing to own and nothing to purge. The one exception
    is deliberately outside this use case: a broker refusal (`ExportNotQueued`, X-22) happens after
    the commit, and the router records that job `failed` / `not_queued` in a second transaction —
    which is the only reason `mark_failed` is legal from `queued` at all.
    """

    def __init__(
        self,
        jobs: ExportJobRepository,
        get_tailoring_run: GetTailoringRun,
        events: EventPublisherPort,
        clock: Clock,
        max_per_session: int = 40,
        max_per_user_run: int = 20,
    ) -> None:
        self._jobs = jobs
        self._get_tailoring_run = get_tailoring_run
        self._events = events
        self._clock = clock
        self._max_per_session = max_per_session
        self._max_per_user_run = max_per_user_run

    async def __call__(self, cmd: RequestExportCommand) -> RequestExportResult:
        # Step 1. The composed read carries the authorization rule and the 404 collapse; this use
        # case never sees a run repository, so it cannot forget either (see the class docstring).
        run = await self._get_tailoring_run(cmd.tailoring_run_id, cmd.requester)

        # Step 2. The aggregate's own status, not `current_documents is not None`: equivalent, but
        # this one says what it means and is the value the error carries to the client (X-14).
        if run.status is not TailoringRunStatus.SUCCEEDED:
            raise TailoringRunNotExportable(run.status)

        # Step 3. **Cross-aggregate, therefore here and not on `ExportJob`.** It compares a job's
        # `run_version` to a *run*'s `version`, and no `ExportJob` instance can see a run;
        # `was_requested_for` is as far as the aggregate can go. Soft on purpose (X-23): two
        # genuinely concurrent requests may both miss, which is cheaper than a lock on a render
        # nobody paid for.
        #
        # The layout is resolved first because it is part of the key (ADR-0016 amendment): a PDF
        # with no choice is the default layout, and a format that takes none keeps `None`. A DOCX
        # *with* a layout is not refused here — it misses the lookup (no DOCX row has a layout) and
        # is refused by `ExportJob.request` at step 5, so the rule lives in one place.
        layout_template = cmd.layout_template
        if layout_template is None and cmd.format.takes_layout_template:
            layout_template = DEFAULT_LAYOUT_TEMPLATE
        existing = await self._jobs.find_latest_for_key(
            run.id, cmd.document, cmd.format, layout_template
        )
        if (
            existing is not None
            and existing.status is not ExportJobStatus.FAILED
            and existing.was_requested_for(run.version)
        ):
            # X-16: 200 with the job already in flight (or already ready). No row, no task.
            return RequestExportResult(export_job=existing, created=False)

        # Step 4. **Cross-aggregate policy, therefore here too.** The rule spans every job the
        # session owns, which no single `ExportJob` can see; reaching for it from inside `request`
        # would mean a repository call in a constructor (ADR-0014 §4). It runs *after* step 3 on
        # purpose: a visitor at the cap who asks again for a job that already exists is handed that
        # job, rather than a 409 that would be true and useless (X-18).
        # The cap is chosen by variant (ADR-0016 amendment (b)): 40 per guest session, 20 per run for
        # a user. The guest cap stays `count_for_session`: it spans every run in the session.
        # Matched on the **run's** owner, which `GetTailoringRun` has just proven equal to the
        # resolved requester — the job will take that owner too (step 5), so the cap and the row
        # agree on whose job this is.
        match run.owner:
            case GuestOwner(guest_session_id=guest_session_id):
                if await self._jobs.count_for_session(guest_session_id) >= self._max_per_session:
                    raise TooManyExportJobs(str(guest_session_id))
            case UserOwner():
                if await self._jobs.count_for_run(run.id) >= self._max_per_user_run:
                    raise TooManyExportJobs(str(run.id))
            case _:
                assert_never(run.owner)

        # Step 5. `ExportFormatNotQueued` propagates from here (X-15) — the second lock behind the
        # boundary's literal type. `run_version` is read off the run, never taken from the caller.
        job = ExportJob.request(
            id=self._jobs.next_identity(),
            # From the run, never from the command (ADR-0016 amendment (a)): *a job's owner is its
            # run's owner* is a cross-aggregate invariant no `ExportJob` can see, so it is kept here,
            # and the ownership graph cannot cross owners (ADR-0022).
            owner=run.owner,
            tailoring_run_id=run.id,
            document=cmd.document,
            format=cmd.format,
            layout_template=layout_template,
            run_version=run.version,
            requested_at=self._clock.now(),
        )

        # Step 6. Add, then publish. **No enqueue** — that is the router's, after the commit
        # (ADR-0014 §5); the class docstring says why the other order is broken rather than merely
        # less tidy.
        await self._jobs.add(job)
        await self._events.publish(*job.release_events())
        return RequestExportResult(export_job=job, created=True)
