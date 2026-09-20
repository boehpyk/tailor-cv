"""The health endpoints — and specifically, what they refuse to lie about.

The readiness endpoint exists because of a documented failure in the previous project: probing only
Postgres and Redis let a crash-looping background worker sit behind a green dashboard for an entire
session. So the interesting test here is not the happy path. It is the one asserting that a stack
with **no Celery worker running** reports 503 — because that is the exact scenario that used to
report 200 (ADR-0005).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tailorcraft.infrastructure.api.deps import get_app_settings, get_clock
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention.heartbeat import PurgeOutcome, RedisPurgeHeartbeat
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import GUEST_PURGE_STALE_AFTER_SECONDS


async def test_liveness_answers_while_the_process_is_up(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"alive": True}


async def test_liveness_does_not_probe_dependencies(client: AsyncClient) -> None:
    """Liveness must stay cheap and dependency-free.

    Docker restarts a container that fails its liveness check. If liveness probed PostgreSQL, a
    brief database blip would kill every application container at once instead of letting them wait
    for the database to come back — turning a thirty-second outage into a restart storm.
    """
    response = await client.get("/health/live")

    assert "dependencies" not in response.json()


class _StubControl:
    def __init__(self, replies: list[dict[str, Any]] | None) -> None:
        self._replies = replies

    def ping(self, timeout: float = 1.0) -> list[dict[str, Any]] | None:
        return self._replies


class _StubCelery:
    def __init__(self, replies: list[dict[str, Any]] | None) -> None:
        self.control = _StubControl(replies)


@pytest.fixture
def _healthy_celery(app: FastAPI) -> None:
    """Pretend a worker is listening.

    Stubbed rather than started: booting a real worker inside the suite would make every test in
    this file depend on a background process, and the behaviour under test — "what does the endpoint
    report given N replies" — is fully determined by the reply list.
    """
    app.state.celery = _StubCelery([{"worker@host": {"ok": "pong"}}])


@pytest.mark.usefixtures("_healthy_celery")
async def test_readiness_reports_every_dependency_by_name(client: AsyncClient) -> None:
    """A bare {"status": "ok"} would look identical whether it probed three dependencies or none."""
    response = await client.get("/health/ready")
    body = response.json()

    assert set(body["dependencies"]) == {"postgres", "redis", "celery"}


@pytest.mark.usefixtures("_healthy_celery")
async def test_readiness_is_200_when_every_dependency_answers(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    body = response.json()

    assert response.status_code == 200, body
    assert body["ready"] is True


async def test_readiness_is_503_when_no_celery_worker_responds(
    client: AsyncClient, app: FastAPI
) -> None:
    """The regression this endpoint exists for.

    A silent, stopped worker is indistinguishable from a healthy system to a check that only asks
    the datastores: the API answers, the database answers, and every export queues forever. Remove
    the Celery probe from `routers/health.py` and this test — and only this test — goes red.
    """
    app.state.celery = _StubCelery([])

    response = await client.get("/health/ready")
    body = response.json()

    assert response.status_code == 503
    assert body["ready"] is False
    assert body["dependencies"]["celery"]["healthy"] is False
    # The other two are genuinely up, and the report says so rather than collapsing to one flag.
    assert body["dependencies"]["postgres"]["healthy"] is True


# ---------------------------------------------------------------------------------------------
# T28 (RED) — the `jobs.guest_purge` contract (ADR-0019, AC-31…AC-34, R-25/R-26).
#
# `_guest_purge_status` (routers/health.py) is a skeleton: the live path calls
# `_skeleton_guest_purge_status`, which always returns
# `{scheduled: False, last_run: None, last_run_age_seconds: None, last_outcome: None,
#   overdue: None, stale: False, detail: "not implemented (T27 skeleton)"}`
# regardless of what `probe_guest_purge` actually computed. Every test below therefore pairs its
# AC's headline assertion with a second field the skeleton cannot get right by coincidence —
# typically `scheduled` (always `False` in the skeleton) or `last_run` (always `None`) — so a red
# here fails **on the assertion**, never on a missing key.
# ---------------------------------------------------------------------------------------------


def _override_settings(app: FastAPI, base: Settings, **updates: object) -> Settings:
    """Point every settings-reading path (the route's `SettingsDep` *and* `app.state.settings`,
    which `probe_guest_purge`'s caller reads the same object from) at one modified `Settings` —
    the pattern `test_intake.py` / `test_posting.py` already use for the same reason."""
    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


async def _write_heartbeat(redis_url: str, *, at: datetime, outcome: PurgeOutcome = "ok") -> None:
    """Write the purge heartbeat directly, the way `test_purge_cli.py` reads it back — through the
    real `RedisPurgeHeartbeat`, never a hand-built hash, so a test failure here can never be "the
    fixture wrote the wrong shape"."""
    redis = create_redis(redis_url)
    try:
        await RedisPurgeHeartbeat(redis).record(
            at=at, outcome=outcome, sessions_deleted=0, files_unlinked=0, duration_ms=0
        )
    finally:
        await redis.aclose()


def _guest_purge(body: dict[str, Any]) -> dict[str, Any]:
    return dict(body["jobs"]["guest_purge"])


@pytest.mark.usefixtures("_healthy_celery")
async def test_jobs_guest_purge_key_set_is_pinned_exactly(client: AsyncClient) -> None:
    """AC-31: `jobs.guest_purge` has exactly these seven keys, no more and no fewer.

    **Green on arrival, and that is not a bug in this test.** `GuestPurgeStatus` (schemas/health.py)
    is a Pydantic model with exactly these seven fields; FastAPI's `response_model` serialization
    enforces the key set on any object that satisfies the model, including the T27 skeleton's
    placeholder. A Pydantic field list *is* its signature (the slice-1.5 export precedent this
    task's brief names) — there is nothing here for T29 to break, and nothing here discriminates
    between the skeleton and the real field-copy. It stays in the suite because AC-31 says the
    shape is a pinned contract, and a future change to `GuestPurgeStatus` should fail this test
    specifically rather than one of the AC-32…AC-34 tests below, which test *values*, not *shape*.
    """
    response = await client.get("/health/ready")
    body = response.json()

    assert set(body["jobs"]["guest_purge"]) == {
        "scheduled",
        "last_run",
        "last_run_age_seconds",
        "last_outcome",
        "overdue",
        "stale",
        "detail",
    }


@pytest.mark.usefixtures("_healthy_celery")
async def test_a_four_hour_old_heartbeat_with_healthy_dependencies_is_200_ready_and_stale(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    clear_redis: None,
) -> None:
    """AC-32, the row that matters: staleness is a fact `jobs` carries and never a reason to fail
    readiness. All three dependencies answer healthy and the heartbeat is 4h old — twice
    `GUEST_PURGE_STALE_AFTER_SECONDS` — so the response must still be 200 with `ready: true`, and
    `stale` must be `true`. Against the skeleton this fails on `stale` (hardcoded `False`), on
    `scheduled` (hardcoded `False`) and on `last_run` (hardcoded `None`) all at once.
    """
    modified = _override_settings(app, settings, guest_purge_enabled=True)
    app.dependency_overrides[get_clock] = lambda: clock
    await _write_heartbeat(modified.redis_url, at=clock.now() - timedelta(hours=4))

    response = await client.get("/health/ready")
    body = response.json()
    guest_purge = _guest_purge(body)

    assert response.status_code == 200, body
    assert body["ready"] is True
    assert guest_purge["stale"] is True
    assert guest_purge["scheduled"] is True
    assert guest_purge["last_run"] is not None


@pytest.mark.usefixtures("_healthy_celery")
async def test_redis_down_is_503_because_of_the_redis_probe_not_the_heartbeat(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    """AC-32's second half: with Redis unreachable the response is 503 **because the Redis
    dependency probe says so**, never because of anything under `jobs`. A test that only checked
    `response.status_code == 503` would pass even if a future edit folded `jobs.guest_purge` into
    the `all(r.healthy for r in results)` line by mistake (ADR-0019 decision 2's exact worry) — so
    this asserts the *other* two dependencies are genuinely healthy, pinning the 503 to Redis alone.

    **Likely green on arrival.** `ready()`'s `all_healthy = all(r.healthy for r in results)` line
    (routers/health.py) is pre-existing code, untouched by the T27/T29 skeleton — `JobStatus` has
    no `healthy` attribute, so `guest_purge` was never eligible for that line in the first place.
    This test does not exercise `_guest_purge_status` at all; it is the regression guard for the
    mechanism ADR-0019 relies on, recorded here because AC-32 asks for it explicitly.
    """
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.get("/health/ready")
    body = response.json()

    assert response.status_code == 503, body
    assert body["ready"] is False
    assert body["dependencies"]["redis"]["healthy"] is False
    assert body["dependencies"]["postgres"]["healthy"] is True
    assert body["dependencies"]["celery"]["healthy"] is True


@dataclass(frozen=True)
class _StaleCase:
    id: str
    scheduled: bool
    heartbeat_age: timedelta | None  # None means "never ran"
    expected_stale: bool


# AC-33's truth table: `stale` iff `scheduled and (last_run is None or age > 3h)`.
_STALE_CASES: list[_StaleCase] = [
    _StaleCase("scheduled_never_run_is_stale", True, None, True),
    _StaleCase(
        "scheduled_just_over_the_bound_is_stale",
        True,
        timedelta(seconds=GUEST_PURGE_STALE_AFTER_SECONDS + 1),
        True,
    ),
    _StaleCase(
        "scheduled_exactly_at_the_bound_is_not_stale",
        True,
        timedelta(seconds=GUEST_PURGE_STALE_AFTER_SECONDS),
        False,
    ),
    _StaleCase("scheduled_recent_is_not_stale", True, timedelta(hours=1), False),
    _StaleCase(
        "unscheduled_forces_not_stale_even_against_an_ancient_heartbeat",
        False,
        timedelta(hours=100),
        False,
    ),
]


@pytest.mark.usefixtures("_healthy_celery")
@pytest.mark.parametrize("case", _STALE_CASES, ids=[c.id for c in _STALE_CASES])
async def test_stale_truth_table(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    clear_redis: None,
    case: _StaleCase,
) -> None:
    """AC-33, driven through `settings.guest_purge_enabled` and a heartbeat written at a controlled
    age — including the row that matters most, `scheduled: false` forcing `stale: false` even
    against a heartbeat that is over four days old.

    Every case pins a second field beside `stale` — `scheduled` for the four `scheduled: true` rows,
    `last_run` for the one `scheduled: false` row — because `stale` alone happens to equal the
    skeleton's hardcoded `False` for two of these five rows (the two `expected_stale=False` cases)
    and would report green there for the wrong reason.
    """
    modified = _override_settings(app, settings, guest_purge_enabled=case.scheduled)
    app.dependency_overrides[get_clock] = lambda: clock
    if case.heartbeat_age is not None:
        await _write_heartbeat(modified.redis_url, at=clock.now() - case.heartbeat_age)

    response = await client.get("/health/ready")
    body = response.json()
    guest_purge = _guest_purge(body)

    assert response.status_code == 200, body
    assert guest_purge["stale"] is case.expected_stale
    assert guest_purge["scheduled"] is case.scheduled
    if case.heartbeat_age is None:
        assert guest_purge["last_run"] is None
    else:
        assert guest_purge["last_run"] is not None


@pytest.mark.usefixtures("_healthy_celery")
async def test_overdue_count_failure_degrades_only_that_field(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    clear_redis: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-34 / R-29: a failed backlog count degrades `overdue` alone. The response stays 200 (the
    other dependencies are healthy) and `detail` is set so an operator knows a field could not be
    computed.

    **The `overdue: null` + non-null `detail` shape alone is green on arrival** — the T27 skeleton
    produces exactly that shape unconditionally, on every request, whether or not anything actually
    failed. Paired here with `scheduled` and `last_run`, which the skeleton hardcodes to `False` /
    `null` regardless of the real `Settings` or the heartbeat this test writes — a skeleton that
    only *resembles* a correct degraded response is caught by those two assertions.
    """
    modified = _override_settings(app, settings, guest_purge_enabled=True)
    app.dependency_overrides[get_clock] = lambda: clock
    await _write_heartbeat(modified.redis_url, at=clock.now())

    async def _boom(self: SqlAlchemyExpiredGuestData, as_of: datetime) -> int:
        raise RuntimeError("simulated overdue-count failure")

    monkeypatch.setattr(SqlAlchemyExpiredGuestData, "count_expired", _boom)

    response = await client.get("/health/ready")
    body = response.json()
    guest_purge = _guest_purge(body)

    assert response.status_code == 200, body
    assert guest_purge["overdue"] is None
    assert guest_purge["detail"] is not None
    assert guest_purge["scheduled"] is True
    assert guest_purge["last_run"] is not None


@pytest.mark.usefixtures("_healthy_celery")
async def test_a_healthy_backlog_does_not_suppress_staleness(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    clear_redis: None,
) -> None:
    """R-25: the runbook's point that the falling **backlog** is the signal to trust, and the
    **heartbeat** is only a convenience — restated as a negative: staleness must not be quietly
    rescued just because `overdue` looks fine. A 4h-old heartbeat is stale regardless of whether
    the count of expired-and-present sessions is currently healthy.
    """
    modified = _override_settings(app, settings, guest_purge_enabled=True)
    app.dependency_overrides[get_clock] = lambda: clock
    await _write_heartbeat(modified.redis_url, at=clock.now() - timedelta(hours=4))

    response = await client.get("/health/ready")
    body = response.json()
    guest_purge = _guest_purge(body)

    assert response.status_code == 200, body
    # The count itself succeeded (contrast the null+detail shape of the failure test above) —
    # staleness is not being reported *because* the count failed.
    assert guest_purge["overdue"] is not None
    assert guest_purge["stale"] is True


@pytest.mark.usefixtures("_healthy_celery")
async def test_no_heartbeat_after_a_redis_flush_reports_three_nulls(
    client: AsyncClient, app: FastAPI, settings: Settings, clear_redis: None
) -> None:
    """R-26: a flushed Redis and a job that has never run are indistinguishable to this probe, and
    both are reported the same honest way — `last_run`, `last_run_age_seconds` and `last_outcome`
    all `null`, with no `detail` (this is not a failure to compute; there is simply nothing to
    report yet). `clear_redis` already guarantees no heartbeat key survives between tests, which is
    exactly the state a genuine flush leaves.

    Paired with `scheduled: true` and `detail: null` — both of which the skeleton cannot produce
    (it hardcodes `scheduled: false` and a non-null placeholder `detail`) — so this is not the
    `last_run: null`-alone shape the skeleton happens to share with the real "never run" case.
    """
    _override_settings(app, settings, guest_purge_enabled=True)

    response = await client.get("/health/ready")
    body = response.json()
    guest_purge = _guest_purge(body)

    assert response.status_code == 200, body
    assert guest_purge["last_run"] is None
    assert guest_purge["last_run_age_seconds"] is None
    assert guest_purge["last_outcome"] is None
    assert guest_purge["scheduled"] is True
    assert guest_purge["detail"] is None
