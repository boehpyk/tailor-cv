"""The `ListExportsForRun` use case: every `ExportJob` requested for one run, newest first, with the
run's current version.

A use case rather than a bare `jobs.list_for_run(run_id)` for the reason every read in this codebase
is one: it carries the authorization rule. Here the rule is **inherited** rather than re-expressed —
the run is authorized through `GetTailoringRunForSession`, and every job of an authorized run
belongs to the same session by construction, because a job is created only by `RequestExport` after
that same check.

That is the one thing to be careful about if this listing ever grows a second entry point: the
guarantee is *the run was authorized*, not *each row was checked*. `list_for_run` is keyed on the
run and not on the session on purpose (see its port docstring), so calling it without authorizing
the run first would return another visitor's jobs with no error anywhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRunForSession
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.ports import ExportJobRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.value_objects import TailoringRunId


@dataclass(frozen=True, slots=True)
class ExportListing:
    """Every job of one run, newest first, plus the version that run is at.

    `run_version` is an `int` and not `int | None` — the contrast with `ExportJobLookup` is
    deliberate and is not an inconsistency to "fix". This use case *authorized* the run in order to
    answer at all, so it is holding the aggregate; `GetExportJobForSession` is reading a **job**,
    and its run may legitimately be gone. Where a value cannot be absent, the type says so.

    One version for the whole listing, not one per job: the caller computes `current` per row with
    `job.was_requested_for(run_version)`, so the comparison lives in one place and every row is
    judged against the same instant's fact.

    An empty `jobs` is an ordinary answer — never a 404. "Nothing has been exported yet" is the
    state of every run that has just finished, and it is what the workspace renders on first paint.
    """

    jobs: Sequence[ExportJob]
    run_version: int


class ListExportsForRun:
    """Every `ExportJob` requested for `run_id`, newest first, for a caller who owns that run.

    **Why this endpoint exists at all**, since a client could poll each job it knows about: a
    browser refresh loses every job id the page was holding. One request keyed on the run — which
    *is* in the URL — lets the workspace reattach to all of them, instead of four polls against ids
    it no longer has. A run has two documents and two queued formats, so four in flight is the
    realistic maximum and the reason this is a list rather than a lookup.

    Raises `GuestSessionNotFound` / `GuestSessionExpired` and `TailoringRunNotFound` — all three
    through the composed `GetTailoringRunForSession`, all with the same collapse of "not mine" into
    "not found" (X-3). **A run that is not `succeeded` is not an error here**: unlike
    `RequestExport` and `RenderDocumentInline`, this use case raises no `TailoringRunNotExportable`,
    because listing the exports of a run that has none is a perfectly good question with the answer
    `[]`. The export *bar* is what checks the status; the listing does not.

    Flow (technical-plan.md, "Application layer" §5; T7 implements it):

    1. ``run = await get_tailoring_run(run_id, guest_session_id)`` — the authorization, inherited.
    2. ``jobs = await self._jobs.list_for_run(run.id)`` — note ``run.id``, the id this use case
       just authorized, never an id the caller supplied. For a listing that *is* the authorization
       rule, and it is enforced by there being nothing else worth passing.
    3. Return ``ExportListing(jobs=jobs, run_version=run.version)``.

    **Newest first is the repository's job**, not a sort here: `list_for_run` orders on
    `requested_at DESC, id DESC` in SQL. Re-sorting in Python would be a second ordering free to
    disagree with the first, and `id DESC` is what breaks the tie between two jobs requested in the
    same whole second — the `Clock` port is whole-second by contract, so ties are ordinary rather
    than rare.
    """

    def __init__(
        self,
        jobs: ExportJobRepository,
        get_tailoring_run: GetTailoringRunForSession,
    ) -> None:
        self._jobs = jobs
        self._get_tailoring_run = get_tailoring_run

    async def __call__(
        self, run_id: TailoringRunId, guest_session_id: GuestSessionId
    ) -> ExportListing:
        # Step 1. The authorization, inherited whole — session resolution, the ownership link, and
        # the collapse of "not mine" into "not found" (X-3). No `TailoringRunNotExportable` here:
        # listing the exports of a run that has none is a good question whose answer is `[]`.
        run = await self._get_tailoring_run(run_id, guest_session_id)

        # Step 2. `run.id` — the id this use case just authorized, never the id the caller supplied.
        # For a listing that *is* the authorization rule, and it is enforced by there being nothing
        # else worth passing.
        jobs = await self._jobs.list_for_run(run.id)

        # Step 3. Newest first is the repository's job, in SQL. Re-sorting here would be a second
        # ordering free to disagree with the first.
        return ExportListing(jobs=jobs, run_version=run.version)
