"""Response schemas for the health endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DependencyStatus(BaseModel):
    """One probed dependency."""

    healthy: bool
    detail: str | None = None


class GuestPurgeStatus(BaseModel):
    """The guest purge, reported as a fact (ADR-0019, AC-31).

    **The key set is the contract and a test pins it.** Seven fields, in the order the ADR writes
    them; this model is the wire shape of `health.probes.JobStatus` and nothing more.

    **Nothing in here can change the status code or `ready`** — the safety lives in the *type* one
    layer down (`JobStatus` has no `healthy` field, so `all(r.healthy …)` cannot reach it), not in a
    promise made here. This model is why the field names are stable; that model is why they are
    harmless.

    **No field has a default.** Every one is supplied explicitly at the single construction site, on
    the house rule `PurgeReport` states: a default on a reported fact is how a second construction
    site publishes a confident `false` for something it never computed. `stale` is the sharp one —
    it is the field an operator acts on.

    **`jobs` is required in this response and optional in the TypeScript client**, and the asymmetry
    is deliberate (R-32): this API always sends it, and during a deploy a browser can be served a
    bundle newer than the API it talks to. The client renders "not reported"; the server never
    omits.

    Nothing here identifies anybody: a flag, an instant, a count, a word, and an exception's type
    (Constitution §8, AC-16, AC-36).
    """

    scheduled: bool
    last_run: str | None
    last_run_age_seconds: int | None
    last_outcome: Literal["ok", "failed"] | None
    overdue: int | None
    stale: bool
    detail: str | None


class JobsStatus(BaseModel):
    """The scheduled jobs this deployment reports.

    A named model rather than `dict[str, …]` (which is what `dependencies` is, one field up) because
    the members do not share a shape: a dependency is always `{healthy, detail}`, while a job
    publishes whatever facts *that* job has. ADR-0019 decision 6 says the two existing beat sweeps
    land here when they grow a heartbeat, and each will bring its own fields — a dict would force
    them into one type or into `Any`.

    **A new member here is never a new way to fail readiness** (ADR-0019 decision 6). That rule is
    worth more than this class: it is the reason the next person adding a job does not have to
    re-litigate the 503 question.
    """

    guest_purge: GuestPurgeStatus


class ReadinessResponse(BaseModel):
    """The readiness report.

    Deliberately names every dependency it checked, rather than collapsing to a single boolean. A
    bare `{"status": "ok"}` tells an operator at 2 a.m. nothing they can act on, and — worse — looks
    identical whether it probed three dependencies or none.

    Since slice 1.6 it carries two *different kinds* of thing, and the split is the point:
    `dependencies` decides `ready` and the status code; `jobs` decides nothing and is read
    (ADR-0019). **A green `/health/ready` no longer means "everything is fine"** — it means this
    process can serve a request. Reading `jobs.guest_purge` is a separate act.
    """

    ready: bool
    dependencies: dict[str, DependencyStatus] = Field(default_factory=dict)
    jobs: JobsStatus


class LivenessResponse(BaseModel):
    """The liveness report: this process is running. It claims nothing else."""

    alive: bool = True
