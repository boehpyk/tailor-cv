"""Response schemas for the health endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DependencyStatus(BaseModel):
    """One probed dependency."""

    healthy: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """The readiness report.

    Deliberately names every dependency it checked, rather than collapsing to a single boolean. A
    bare `{"status": "ok"}` tells an operator at 2 a.m. nothing they can act on, and — worse — looks
    identical whether it probed three dependencies or none.
    """

    ready: bool
    dependencies: dict[str, DependencyStatus] = Field(default_factory=dict)


class LivenessResponse(BaseModel):
    """The liveness report: this process is running. It claims nothing else."""

    alive: bool = True
