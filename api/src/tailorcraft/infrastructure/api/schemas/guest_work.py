"""Response schema for `POST /api/me/guest-work/claim` (slice 2.4, technical plan §4).

**Counts only.** `files_unlinked` and the unlink failures in `GuestWorkClaimReport` are the
operator's (the `identity.guest_work_claimed` log line): a user can do nothing with them, so they
never reach the wire. All zeros is a success, not an error — "nothing to claim" (no cookie, an unknown
or already-claimed one, an expired session) answers 200 so a retried claim whose first response was
lost does not read as a failure (§4, idempotency).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GuestWorkClaimResponse(BaseModel):
    """`{"base_cvs", "job_postings", "tailoring_runs", "export_jobs", "working_copies_dropped"}` —
    exactly these five keys (AC-24), each a non-negative count."""

    model_config = ConfigDict(extra="forbid")

    base_cvs: int = Field(ge=0)
    job_postings: int = Field(ge=0)
    tailoring_runs: int = Field(ge=0)
    export_jobs: int = Field(ge=0)
    working_copies_dropped: int = Field(ge=0)
