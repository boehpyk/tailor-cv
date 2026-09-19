"""The `GetExportJobForSession` use case: read one `ExportJob`, authorized by the link to its
session, together with the run's version *now*.

A use case rather than `jobs.get(id)` called straight from a router, **because it carries the
authorization rule** (ADR-0008, ADR-0010):

    What authorizes access to an export job is **the link** — `job.guest_session_id == the resolved
    session id` — checked here, on every read. Owning a session id is not authority over an object
    that references it: a guest session is not a login, and the id itself proves nothing about
    which rows it may see.

The check lives here rather than in a router so that a **second entry point** cannot reach an
`ExportJob` without it. This context has three onto one job — the poll, the download and the list —
and a check duplicated in every caller is a check one caller eventually forgets. The download's is
inherited by composition: `DownloadExportFile` calls this use case rather than the repository.

`ExportJobRepository.get` deliberately does **not** filter by session. A repository that silently
filtered would make the rule invisible at the call site, and invisible rules are the ones a fourth
entry point forgets.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.identity.resolve_guest_session import (
    resolve_active_guest_session,
)
from tailorcraft.domain.export.errors import ExportJobNotFound, ExportJobNotOwnedBySession
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.ports import ExportJobRepository
from tailorcraft.domain.export.value_objects import ExportJobId
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.tailoring.ports import TailoringRunRepository


@dataclass(frozen=True, slots=True)
class ExportJobLookup:
    """One job, plus the version its run is at **right now**.

    Two values rather than one because `current` — the flag the API serializes and the UI reads to
    decide between *Download* and *Export again* — is a **cross-aggregate** comparison
    (`job.was_requested_for(run_version_now)`), and neither aggregate can make it alone. The job
    knows the version it was requested for; only the run knows the version it is at. Returning the
    second alongside the first is what lets the boundary compute the flag without reaching for a
    repository of its own.

    `run_version_now` is `int | None`. `None` means the run is **gone** — purged, or deleted with
    its session — and the boundary renders `current: false`: a job whose source no longer exists is
    certainly not current, and a missing run is not an error on a read of a job that still exists.
    It is not folded to `current: false` here, because this use case's job is to report facts and
    the flag is the boundary's rendering of them; a `bool` in this field would also make the
    "run is gone" case indistinguishable from "the run moved on", which is a distinction the poll's
    log line wants.

    Deliberately **not** the whole `TailoringRun`. All the caller needs is one integer, and
    handing back a run would put a second aggregate — with two document bodies on it — into the
    response path of a poll that fires every two seconds.
    """

    job: ExportJob
    run_version_now: int | None


class GetExportJobForSession:
    """Look up an `ExportJob` by id, but only if it belongs to `guest_session_id`, and report the
    run's current version alongside it.

    Raises `GuestSessionNotFound` / `GuestSessionExpired` if the session itself no longer resolves —
    the same defense-in-depth every other read use case applies, repeated here so this one is safe
    to call from anywhere and not only from behind the API's cookie dependency.

    Raises `ExportJobNotFound` in **two** situations that must be indistinguishable from outside
    (X-43): the id does not exist at all, and the id exists but names a job owned by a *different*
    session. This use case never raises `ExportJobNotOwnedBySession` to its caller and the API never
    maps a 403 — a distinguishable "wrong owner" response would confirm to someone guessing ids
    that the id exists, which is exactly what a 404 is supposed to withhold. That matters as much
    here as for a run: **an export job id is a polling handle and a download handle**, so it is the
    id an attacker is most likely to be enumerating, and the thing behind it is a file.

    `ExportJobNotOwnedBySession` still exists as a type, and T7 raises
    ``ExportJobNotFound(...) from ExportJobNotOwnedBySession(...)``: this use case's own tests need
    to tell "absent" from "not mine" apart even though the boundary must not, and `__cause__` is
    where that distinction survives without ever crossing the wire. `GetTailoringRunForSession`
    does the identical thing for a run, and 1.3's tests read `__cause__` the same way.

    Flow (technical-plan.md, "Application layer" §4; T7 implements it):

    1. ``session = await resolve_active_guest_session(sessions, clock, guest_session_id)``.
    2. ``job = await jobs.get(job_id)`` — `ExportJobNotFound` if there is no such row.
    3. ``if job.guest_session_id != session.id:`` raise the collapsed `ExportJobNotFound`, chained
       from `ExportJobNotOwnedBySession`.
    4. ``run = await runs.find(job.tailoring_run_id)`` — **`find`, not `get`**: a run that has gone
       is an ordinary answer here rather than an exception, because the job is the thing being
       read and it still exists. Return ``ExportJobLookup(job, run.version if run else None)``.

    **Why the run is read at all on a poll.** The alternative is a boundary that computes `current`
    from a version the client sends back, which would let a client declare its own export current;
    or one that omits the flag, which pushes the comparison into the UI and re-implements a
    business rule in TypeScript. One primary-key lookup per poll is the price of neither.
    """

    def __init__(
        self,
        jobs: ExportJobRepository,
        runs: TailoringRunRepository,
        sessions: GuestSessionRepository,
        clock: Clock,
    ) -> None:
        self._jobs = jobs
        self._runs = runs
        self._sessions = sessions
        self._clock = clock

    async def __call__(
        self, job_id: ExportJobId, guest_session_id: GuestSessionId
    ) -> ExportJobLookup:
        # Step 1. Defence in depth: the API's cookie dependency has already resolved this session,
        # and this use case is still safe to call from anywhere because it resolves it again.
        session = await resolve_active_guest_session(self._sessions, self._clock, guest_session_id)

        # Step 2. `get`, not `find`: an id that names nothing is an error on a read, and the
        # repository raises the same type step 3 raises.
        job = await self._jobs.get(job_id)

        if job.guest_session_id != session.id:
            # "Not mine" must be indistinguishable from "does not exist" at this boundary (X-43,
            # AC-24): the public exception is `ExportJobNotFound`, the same type `jobs.get` raises
            # for an id that was never issued, because a 403 here would confirm to someone
            # enumerating handles that the id is real — and an export job id is both a polling
            # handle and a download handle, so it is the id most worth guessing, with a file
            # behind it. The distinction survives only on `__cause__`, where this use case's own
            # tests can see it and nothing that crosses the wire can.
            raise ExportJobNotFound(str(job_id)) from ExportJobNotOwnedBySession(str(job_id))

        # Step 4. **`find`, not `get`**: a run that has gone is an ordinary answer here rather than
        # an exception, because the job is the thing being read and it still exists. `None` becomes
        # `run_version_now=None`, which the boundary renders as `current: false` — a job whose run
        # is gone is certainly not the document as it stands.
        run = await self._runs.find(job.tailoring_run_id)
        return ExportJobLookup(job=job, run_version_now=run.version if run is not None else None)
