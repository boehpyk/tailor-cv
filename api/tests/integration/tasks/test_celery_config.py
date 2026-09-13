"""Tests for the Celery application's configuration (`infrastructure/tasks/app.py`), V5f, /verify
round 1, test-after.

No broker, no database — every assertion here inspects the module-level `app` object `create_celery`
built at import time, the same object both the worker and beat actually run with. Three things this
file is not:

- **Not the sweep's own behaviour** — that is `test_tailoring_sweep_task.py`.
- **Not the queue routing keys' behaviour either** — those are a *sibling* concern to
  `task_queues`'s mere existence, and get their own section below (V5d-3, `95ca5bc`) because they
  were the one thing measured wrong at /verify round 1: two `Queue`s declared with no `routing_key`
  both bind the `celery` exchange under the SAME key, so a publish to either queue's name reaches
  BOTH of them (`tasks/app.py`'s own comment on `task_queues`).
- **Not a claim about what a deployed worker's `Settings` will actually hold** — the one item below
  that touches `tailoring_stale_after_seconds` says explicitly where it stops being able to prove
  anything, because that field is overridable from the environment and no object in this process can
  see a value chosen by a future deployment.
"""

from __future__ import annotations

import pytest

from tailorcraft.infrastructure.settings import MisconfiguredSettings, Settings
from tailorcraft.infrastructure.tasks import app as tasks_app_module
from tailorcraft.infrastructure.tasks.app import (
    ABANDON_STALE_TAILORING_RUNS_TASK_NAME,
    STALE_RUN_SWEEP_EXPIRES_SECONDS,
    STALE_RUN_SWEEP_INTERVAL_SECONDS,
    app,
)
from tailorcraft.infrastructure.tasks.tailoring_sweep import abandon_stale_tailoring_runs

# --- The beat schedule -----------------------------------------------------------------------------


def test_beat_schedule_registers_the_sweep_every_60_seconds_with_an_expiry_under_60() -> None:
    entry = app.conf.beat_schedule["abandon-stale-tailoring-runs"]

    assert entry["task"] == ABANDON_STALE_TAILORING_RUNS_TASK_NAME, (
        "a schedule and a task that disagree about the name produce NotRegistered on the worker, "
        "once a minute, in a log nobody reads (tasks/app.py's own comment)"
    )
    assert entry["schedule"] == STALE_RUN_SWEEP_INTERVAL_SECONDS == 60.0
    assert entry["options"]["expires"] == STALE_RUN_SWEEP_EXPIRES_SECONDS
    assert entry["options"]["expires"] < entry["schedule"], (
        "the expiry must sit below the interval so at most one live tick exists at any moment — "
        "otherwise a worker back after an outage finds every missed tick still queued ahead of real "
        "work (tasks/app.py's own comment)"
    )


def test_the_sweep_tasks_own_registered_name_matches_the_shared_constant() -> None:
    """(verify round 2) The test above only reads `beat_schedule`'s OWN copy of the name back and
    compares it to the same constant it was built from — it cannot tell a correctly-named
    `@app.task(name=...)` from one that silently drifted to some other literal, because both sides
    of that comparison would still be the schedule's copy against the shared constant. This test
    reads the DECORATOR's own result instead — `abandon_stale_tailoring_runs.name`, what a real
    dispatch actually looks up — and that the worker's own `app.tasks` registry holds an entry under
    that name, not merely under whatever `beat_schedule` claims.

    Proven to discriminate by locally changing `tasks/tailoring_sweep.py`'s
    `@app.task(name=ABANDON_STALE_TAILORING_RUNS_TASK_NAME, ...)` to a typo'd string literal: this
    test's first assertion goes red (the decorator's name no longer equals the constant), which the
    schedule-comparison test above cannot catch because nothing here touches `beat_schedule` at all.
    """
    assert abandon_stale_tailoring_runs.name == ABANDON_STALE_TAILORING_RUNS_TASK_NAME
    assert ABANDON_STALE_TAILORING_RUNS_TASK_NAME in app.tasks, (
        "a task name absent from the worker's own registry is NotRegistered on every dispatch, "
        "beat's included — the exact failure mode this constant exists to prevent"
    )


# --- The Redis redelivery window ---------------------------------------------------------------


def test_broker_visibility_timeout_exceeds_the_hard_task_time_limit() -> None:
    assert app.conf.broker_transport_options["visibility_timeout"] > app.conf.task_time_limit, (
        "a visibility timeout below the hard time limit would let Redis redeliver a HEALTHY task's "
        "message while it is still legitimately running, duplicating it (tasks/app.py's own comment "
        "on broker_transport_options)"
    )


def test_task_reject_on_worker_lost_is_unset_so_the_sweep_is_the_one_recovery_mechanism() -> None:
    assert not app.conf.task_reject_on_worker_lost, (
        "task_reject_on_worker_lost must stay unset/falsy: setting it would redeliver a killed "
        "worker's message straight into ExecuteTailoringRun step 3, which finds the run still "
        "RUNNING and inside the stale window and returns SKIPPED — recovering nothing — while also "
        "risking a redelivery loop for a message that reliably kills its child. The stale-run sweep "
        "is meant to be the ONE recovery mechanism for a lost worker (tasks/app.py's own comment on "
        "task_acks_late)"
    )


# --- The sweep is published to a queue a worker actually consumes ------------------------------


def test_the_sweep_routes_to_a_queue_declared_in_task_queues() -> None:
    entry = app.conf.beat_schedule["abandon-stale-tailoring-runs"]
    routed_queue_name = entry["options"]["queue"]
    declared_queue_names = {queue.name for queue in app.conf.task_queues}

    assert routed_queue_name in declared_queue_names, (
        "a task published to a queue no worker consumes queues forever behind a system that looks "
        "entirely healthy: the API answers, Redis is up, the worker reports itself idle, and nothing "
        "anywhere logs an error (tasks/app.py's own comment on task_queues). Discriminates by "
        "renaming the beat entry's 'queue' option locally without adding a matching Queue()."
    )


# --- V5d-3: every queue's own routing key ------------------------------------------------------


def test_every_queue_in_task_queues_has_a_routing_key_equal_to_its_own_name() -> None:
    """A `Queue` declared with no explicit `routing_key` falls back to `task_default_routing_key`
    ('celery'), so before V5d-3 (`95ca5bc`) both queues bound the `celery` exchange under the SAME
    key — kombu's lookup for `exchange='celery', routing_key='celery'` returned `['celery',
    'tailoring']`, measured at /verify round 1 — and a publish made with that pair landed a message
    in BOTH queues: two deliveries of one tailoring run, and a read-then-write idempotency race that
    could pay for the model twice. Proven to discriminate by dropping `routing_key=...` from either
    `Queue(...)` call in `tasks/app.py` locally, which falls the dropped queue's `routing_key` back
    to `'celery'` and turns this assertion red for that queue (and the next one red too).
    """
    for queue in app.conf.task_queues:
        assert queue.routing_key == queue.name, (
            f"queue {queue.name!r} has routing_key {queue.routing_key!r} — every queue here must "
            "route on its own name, never kombu's default routing key ('celery'), or a publish can "
            "land in more than the one queue it named (measured at /verify round 1)"
        )


def test_no_two_queues_in_task_queues_share_a_routing_key() -> None:
    routing_keys = [queue.routing_key for queue in app.conf.task_queues]

    assert len(routing_keys) == len(set(routing_keys)), (
        "two queues sharing a routing key both receive any publish made with that key — exactly the "
        "double-delivery V5d-3 fixes (tasks/app.py's own comment on task_queues)"
    )


# --- RED (verify round 2): tailoring_stale_after_seconds must stay above task_time_limit, or the --
# --- sweep can abandon a call that is genuinely still in flight — and only a COMMENT enforced -----
# --- that before now -------------------------------------------------------------------------------
#
# Replaces `test_the_default_stale_window_stays_above_the_hard_task_time_limit` (removed here): that
# test could only ever prove the shipped DEFAULTS happen to agree (300 > 180) — it could not catch a
# deployment that sets `TAILORING_STALE_AFTER_SECONDS=100` in its environment, because neither
# number is stored anywhere a test running in THIS process could read a future deployment's value
# back from. Decided at /verify round 2: `create_celery` itself must refuse to build, with
# `MisconfiguredSettings`, whenever the relationship does not hold — in every environment, not only
# production, because the sweep runs against whatever `Settings` the worker holds regardless of
# `app_env`. That turns the comment into something a bad deployment cannot silently violate.
#
# `create_celery` takes no argument and reads `get_settings()` itself (`tasks/app.py`); patched here
# in this module's own namespace, `tasks_app_module`, the same seam-patching technique
# `test_tailoring_sweep_task.py` uses for `container.get_settings`.


def test_create_celery_refuses_a_stale_window_not_safely_above_the_hard_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale window of 100s, well under `task_time_limit` (180): a live call killed by the hard
    limit at 180s would have already been swept and recorded `abandoned` at 100s, while the worker
    may still genuinely be holding it. Mirrors `test_settings.py`'s own guard-message shape: the
    message must name the setting that failed and carry no secret from a sibling field, the same
    property `MisconfiguredSettings` exists for (see that class's own docstring).

    RED today on "DID NOT RAISE <MisconfiguredSettings>": `create_celery` builds unconditionally,
    with nothing yet checking this relationship at all.
    """
    settings = Settings(app_env="test", tailoring_stale_after_seconds=100)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)

    with pytest.raises(MisconfiguredSettings) as exc_info:
        tasks_app_module.create_celery()

    message = str(exc_info.value)
    assert "tailoring_stale_after_seconds" in message
    assert "task_time_limit" in message
    for secret in (settings.database_url, settings.redis_url, settings.celery_broker_url):
        assert secret not in message


def test_create_celery_refuses_a_stale_window_exactly_equal_to_the_hard_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary itself: a stale window EQUAL to `task_time_limit` (180) must still be refused.
    Equality is not "safely above" — a call killed by the hard limit at second 180 and a sweep tick
    judging the same run stale at that same instant is exactly the race this guard exists to rule
    out, not a value that happens to be numerically fine.

    RED today for the same reason as the test above: nothing yet checks this relationship.
    """
    settings = Settings(app_env="test", tailoring_stale_after_seconds=180)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)

    with pytest.raises(MisconfiguredSettings):
        tasks_app_module.create_celery()


def test_create_celery_accepts_a_stale_window_one_second_above_the_hard_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same boundary: 181 is the smallest value that must be ACCEPTED. Not
    red today — `create_celery` already builds successfully for any value, including this one — but
    stated here so the boundary is pinned from both directions in the same commit, rather than only
    from the refusing side."""
    settings = Settings(app_env="test", tailoring_stale_after_seconds=181)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)

    built = tasks_app_module.create_celery()

    assert built.conf.task_time_limit == 180
