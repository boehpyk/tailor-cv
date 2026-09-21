"""Tests for the guest-purge beat entry and the Celery task that runs it
(`infrastructure/tasks/app.py`'s `_guest_purge_schedule` and `infrastructure/tasks/retention.py`'s
`purge_expired_guest_sessions`) — AC-25 … AC-29, written at `/verify`'s MAJOR 1 finding.

Before this file, nothing under `api/tests/` referenced `PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME`,
`_guest_purge_schedule` or `purge_expired_guest_sessions` at all: the single control keeping this
codebase's first `DELETE` off a schedule before the rehearsal (`GUEST_PURGE_ENABLED`, default
`false`) had no test proving it actually gates anything.

**No broker, no database, no Redis** — every assertion here inspects either the pure
`_guest_purge_schedule(settings)` helper or a `Celery` object `create_celery()` built at import time
or against a monkeypatched `Settings`, exactly as `test_celery_config.py`'s own `MisconfiguredSettings`
tests do (`monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)`).

**Follows `test_celery_config.py`'s own precedent for the decorator/registry check**
(`test_the_sweep_tasks_own_registered_name_matches_the_shared_constant`, that file's line 53): a test
that only compares `beat_schedule`'s copy of a name against the same constant it was built from cannot
tell a correctly-named `@app.task(name=...)` from one that silently drifted, because both sides of
that comparison would still be the schedule's copy against the shared constant. This file reads the
**decorator's own** `purge_expired_guest_sessions.name` instead, and that the worker's own `app.tasks`
registry — not `beat_schedule` — holds an entry under that name.

AC-28 and AC-29 are asserted by walking `tasks/retention.py`'s own AST rather than by importing the
module and inspecting `sys.modules` or the built `Task` object's runtime attributes, for the same
reason `tests/unit/retention/test_import_allowlist.py` reads `domain/retention/`'s own source instead
of what ends up transitively loaded: a module-level import of `tasks/container.py` (which the task
legitimately needs, to call the use case) pulls in repositories and `LocalFileStore` *transitively*,
and a `sys.modules`-after-import check cannot distinguish that from `tasks/retention.py` naming one
*directly*. Reading only the `ast.Import` / `ast.ImportFrom` nodes literally inside
`tasks/retention.py`, and only the keyword arguments literally inside its own `@app.task(...)` call,
is the only way to ask what this one module itself declares.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import app as tasks_app_module
from tailorcraft.infrastructure.tasks import retention as retention_task_module
from tailorcraft.infrastructure.tasks.app import (
    DEFAULT_QUEUE_NAME,
    GUEST_PURGE_EXPIRES_SECONDS,
    GUEST_PURGE_INTERVAL_SECONDS,
    PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME,
    _guest_purge_schedule,
)
from tailorcraft.infrastructure.tasks.retention import purge_expired_guest_sessions

_BEAT_ENTRY_NAME = "purge-expired-guest-sessions"


def _settings(*, guest_purge_enabled: bool) -> Settings:
    return Settings(app_env="test", guest_purge_enabled=guest_purge_enabled)


def _retention_task_source() -> str:
    return Path(retention_task_module.__file__).read_text()


# --- AC-25: `_guest_purge_schedule` itself — absent when off, present when on ------------------


def test_guest_purge_schedule_is_empty_when_the_flag_is_off() -> None:
    assert _guest_purge_schedule(_settings(guest_purge_enabled=False)) == {}


def test_guest_purge_schedule_contains_the_entry_when_the_flag_is_on() -> None:
    schedule = _guest_purge_schedule(_settings(guest_purge_enabled=True))

    assert _BEAT_ENTRY_NAME in schedule
    entry = schedule[_BEAT_ENTRY_NAME]
    assert entry["task"] == PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME


# --- AC-25: the same, through `create_celery()` — proves the `**` merge actually wires it in ----


def test_create_celery_beat_schedule_has_no_purge_entry_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GUEST_PURGE_ENABLED` defaults false, and `create_celery` must not register the entry.

    Proven to discriminate by locally dropping the `if not settings.guest_purge_enabled: return {}`
    guard in `_guest_purge_schedule`: this assertion goes red (the entry appears unconditionally)
    while `test_guest_purge_schedule_is_empty_when_the_flag_is_off` above would also go red for the
    same edit — this test additionally proves the merge into `create_celery`'s own `beat_schedule`
    dict is not itself the bug (e.g. a stray unconditional literal entry written beside
    `**_guest_purge_schedule(settings)`).
    """
    settings = _settings(guest_purge_enabled=False)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)

    built = tasks_app_module.create_celery()

    assert _BEAT_ENTRY_NAME not in built.conf.beat_schedule


def test_create_celery_beat_schedule_has_the_purge_entry_when_the_flag_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(guest_purge_enabled=True)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)

    built = tasks_app_module.create_celery()

    assert _BEAT_ENTRY_NAME in built.conf.beat_schedule
    entry = built.conf.beat_schedule[_BEAT_ENTRY_NAME]
    assert entry["task"] == PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME, (
        "a schedule and a task that disagree about the name produce NotRegistered on the worker, "
        "hourly, in a log nobody reads (tasks/app.py's own comment)"
    )


# --- AC-26: hourly, with the expiry below the interval ------------------------------------------


def test_the_purge_entry_runs_hourly_with_an_expiry_below_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(guest_purge_enabled=True)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)
    built = tasks_app_module.create_celery()
    entry = built.conf.beat_schedule[_BEAT_ENTRY_NAME]

    assert entry["schedule"] == GUEST_PURGE_INTERVAL_SECONDS == 3600.0
    assert entry["options"]["expires"] == GUEST_PURGE_EXPIRES_SECONDS == 3500.0
    assert entry["options"]["expires"] < entry["schedule"], (
        "the expiry must sit below the interval so at most one live tick exists at any moment — "
        "otherwise a worker back after an outage finds every missed hourly tick still queued ahead "
        "of real work"
    )


# --- AC-27: the default `celery` queue, and `task_queues` itself untouched ----------------------


def test_the_purge_entry_publishes_to_the_default_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(guest_purge_enabled=True)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)
    built = tasks_app_module.create_celery()
    entry = built.conf.beat_schedule[_BEAT_ENTRY_NAME]

    assert entry["options"]["queue"] == DEFAULT_QUEUE_NAME == "celery"


def test_task_queues_declares_exactly_the_same_three_queues_with_the_purge_schedule_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-27: enabling the third beat entry must add no fourth queue and no fourth kombu binding —
    `task_queues` stays byte-for-byte `celery`, `tailoring`, `export`, exactly as it is with the flag
    off (`test_celery_config.py`'s own `test_task_queues_declares_exactly_the_default_tailoring_and_
    export_queues`). Proven to discriminate by adding a fourth `Queue(...)` to `task_queues` gated on
    `settings.guest_purge_enabled`: this test's set comparison goes red while the un-gated sibling
    test in `test_celery_config.py` (built against the flag-off default `app`) stays green, since it
    never enables the flag at all.
    """
    settings = _settings(guest_purge_enabled=True)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)
    built = tasks_app_module.create_celery()

    queue_names = {queue.name for queue in built.conf.task_queues}

    assert queue_names == {
        DEFAULT_QUEUE_NAME,
        settings.tailoring_queue_name,
        settings.export_queue_name,
    }
    assert len(built.conf.task_queues) == 3


# --- The decorator's own name, and the worker's own registry ------------------------------------


def test_the_purge_tasks_own_registered_name_matches_the_shared_constant() -> None:
    """Mirrors `test_celery_config.py`'s `test_the_sweep_tasks_own_registered_name_matches_the_
    shared_constant` for the purge task. Reads `purge_expired_guest_sessions.name` — the decorator's
    own result, what a real dispatch actually looks up — and `tasks_app_module.app.tasks`, the real
    module-level app both the worker and beat run with (the task is registered **unconditionally**,
    regardless of `GUEST_PURGE_ENABLED`: `tasks/app.py`'s own comment on `include=[...]` says a
    worker that skipped the import would reject a message published by a beat that has the flag on).

    Proven to discriminate by locally changing `tasks/retention.py`'s `@app.task(name=...)` to a
    typo'd string literal: this test's first assertion goes red, which a test that only reads
    `beat_schedule`'s copy of the constant cannot catch.
    """
    assert purge_expired_guest_sessions.name == PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME
    assert PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME in tasks_app_module.app.tasks, (
        "a task name absent from the worker's own registry is NotRegistered on every dispatch, "
        "beat's included — the exact failure mode this constant exists to prevent"
    )


# --- AC-28: a thin entry point — no repository, no FileStore, named directly --------------------


def test_the_purge_task_module_imports_no_repository_and_no_file_store() -> None:
    """AC-28. `tasks/retention.py`'s own imports — read from its source, never from `sys.modules` —
    must name no repository and no `FileStore`: resolving those is `tasks/container.py`'s job, the
    composition root, not the entry point's. A `sys.modules` check after import would also see what
    `container.py` pulls in transitively (it legitimately imports both), which is not the same claim
    as this module naming one directly.

    Proven to discriminate by adding, say, `from tailorcraft.infrastructure.files.local_file_store
    import LocalFileStore` to `tasks/retention.py` locally: the offending-names assertion goes red.
    """
    tree = ast.parse(_retention_task_source())
    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
                imported_names.add((alias.asname or alias.name).rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)

    offending_modules = {m for m in imported_modules if "repositories" in m.lower()}
    offending_names = {
        n for n in imported_names if "repository" in n.lower() or "filestore" in n.lower()
    }

    assert not offending_modules, (
        f"tasks/retention.py imports a repository module directly: {offending_modules}"
    )
    assert not offending_names, (
        f"tasks/retention.py names a repository or FileStore type directly: {offending_names}"
    )


# --- AC-29: no Celery retry -----------------------------------------------------------------------


def test_the_purge_task_declares_no_retry_policy() -> None:
    """AC-29: no `autoretry_for`, no `retry_backoff`, no `max_retries` on the `@app.task(...)` call
    — the next hourly tick is the retry, and the run is idempotent (AC-15). Read from the decorator
    call's own keyword arguments in `tasks/retention.py`'s source, so a retry kwarg added anywhere
    else in the file (it would have to be on this one call to have any effect) cannot hide from it.

    Proven to discriminate by adding `max_retries=3` to `tasks/retention.py`'s `@app.task(...)` call
    locally: the offending-keywords assertion goes red.
    """
    tree = ast.parse(_retention_task_source())
    task_decorator_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "task"
    ]
    assert task_decorator_calls, "expected an @app.task(...) decorator in tasks/retention.py"

    forbidden = {"autoretry_for", "retry_backoff", "max_retries"}
    for call in task_decorator_calls:
        kwarg_names = {kw.arg for kw in call.keywords if kw.arg is not None}
        offending = kwarg_names & forbidden
        assert not offending, f"tasks/retention.py's @app.task(...) declares {offending}"
