"""The `DownloadExportFile` use case: hand back the bytes of a `ready` `ExportJob`'s file, to the
session that owns it.

It composes `GetExportJobForSession` rather than reaching for `ExportJobRepository`, which is the
same inheritance-of-a-rule that `RequestExport` gets from `GetTailoringRunForSession`: the
ownership check and the collapse of "not mine" into 404 are written once, in the read use case, and
this — the second entry point onto a job — gets them for free. A download that authorized itself
would be a fourth copy of a rule, guarding the one thing in this slice that is actually a file.
"""

from __future__ import annotations

from tailorcraft.application.export.get_export_job import GetExportJobForSession
from tailorcraft.domain.export.errors import ExportNotReady
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportJobId, ExportJobStatus
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.files import FileStorePort


class DownloadExportFile:
    """The job and its bytes, for a `ready` job the caller owns.

    Flow (technical-plan.md, "Application layer" §6; T7 implements it):

    1. ``lookup = await get_export_job(job_id, guest_session_id)`` — `GuestSessionExpired`,
       and `ExportJobNotFound` for both "absent" and "not mine" (X-43), inherited whole.
    2. ``if lookup.job.status is not ExportJobStatus.READY: raise ExportNotReady(job.status,
       job.failure_reason)`` — 409 for a `queued` or `rendering` job (X-44) and for a `failed` one
       (X-45), with the reason carried so the boundary can render the failure copy rather than a
       generic "not ready". An honest client never sends either: the control is polled into
       existence. A `ready` job whose run has moved on (`current: false`) is **served**, not
       refused (X-46) — the file is the user's own and refusing it buys nothing; the UI is what
       declines to offer it, and offers *Export again* instead.
    3. ``data = await files.get(lookup.job.storage_ref)`` — **`storage_ref`, the computed key, not
       `file_key`, the recorded one.** They are equal after `mark_ready` and AC-5 asserts it rather
       than assuming it; using the computation keeps this path working from an id alone and keeps
       `file_key | None` from needing a narrowing that only a `ready` status justifies.
    4. Return the job and the bytes.

    **`StoredFileMissing` and `FileStoreUnavailable` propagate**, deliberately and without a
    `mark_failed` anywhere near them. ADR-0014 §2's question — *was anything spent, and is there an
    artifact to own?* — has already been answered for this job: it rendered, it cost a worker
    second, and it is recorded `ready`. A read that cannot find the file is a fact about the
    **store**, not a new outcome of the render, and rewriting the row from a `GET` would make a
    download mutate the thing it downloads. The router separates the two: `StoredFileMissing` →
    **410 `export_file_gone`** (it will not come back, and retrying the download helps nobody —
    re-exporting does, X-47), `FileStoreUnavailable` → **503** (X-48). The subclass relationship is
    what makes the order of those two `except` clauses load-bearing; `domain/shared/files.py` says
    why the pair is a subclass rather than a sibling.

    **This use case writes nothing at all** — no row, no event, no counter. "Downloaded" is a
    client-side state, and a `GET` that recorded one would be a write on the read path, on a table
    the purge then carries, for a fact nothing in the product asks.

    **The return type is a plain tuple, and that contradicts this package's own convention** —
    every other use case here returns a named frozen dataclass — so the reason is at the point of
    the contradiction rather than left for a reader to "fix": the technical plan specifies
    ``-> (job, bytes)``, the two values are of obviously different types and are consumed in one
    unpacking line at the single call site, and a two-field result whose fields would be named
    `job` and `data` names nothing the types do not already say. The named types elsewhere all earn
    their name by carrying a value a reader could otherwise misread — a `bool` that decides 200 vs
    202, an `int | None` that decides `current`, a `Sequence` beside an `int`.
    """

    def __init__(
        self,
        get_export_job: GetExportJobForSession,
        files: FileStorePort,
    ) -> None:
        self._get_export_job = get_export_job
        self._files = files

    async def __call__(
        self, job_id: ExportJobId, guest_session_id: GuestSessionId
    ) -> tuple[ExportJob, bytes]:
        # Step 1. `GuestSessionExpired`, and `ExportJobNotFound` for both "absent" and "not mine"
        # (X-43), inherited whole from the composed read.
        lookup = await self._get_export_job(job_id, guest_session_id)
        job = lookup.job

        # Step 2. 409 for a `queued` or `rendering` job (X-44) and for a `failed` one (X-45), with
        # the reason carried so the boundary can render the failure copy rather than a generic "not
        # ready". A `ready` job whose run has moved on (`current: false`) is **served**, not refused
        # (X-46): the file is the user's own, and refusing it buys nothing — the UI is what declines
        # to offer it, and offers *Export again* instead. Note there is no `current` check here at
        # all, which is that decision made by omission.
        if job.status is not ExportJobStatus.READY:
            raise ExportNotReady(job.status, job.failure_reason)

        # Step 3. **`storage_ref`, the computed key, not `file_key`, the recorded one.** They are
        # equal after `mark_ready` and AC-5 asserts it rather than assuming it; using the
        # computation keeps this path working from an id alone and keeps `file_key | None` from
        # needing a narrowing that only a `ready` status justifies.
        #
        # `StoredFileMissing` and `FileStoreUnavailable` **propagate**, deliberately and with no
        # `mark_failed` anywhere near them: a read that cannot find the file is a fact about the
        # *store*, not a new outcome of the render, and rewriting the row from a `GET` would make a
        # download mutate the thing it downloads. The router separates them — 410 vs 503 (X-47,
        # X-48) — and the subclass relationship is what makes the order of those two `except`
        # clauses load-bearing there.
        data = await self._files.get(job.storage_ref)
        return job, data
