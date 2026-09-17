"""Unit tests for `Settings`' own validation (AC-32/G-32's boot guard), and for the reason
`MisconfiguredSettings` exists rather than a bare `ValueError` (see that class's own docstring).

Pure: no database, no event loop, no fixtures beyond what `Settings(...)` itself needs. `Settings()`
reads `.env` by default, so every test below passes every field it cares about explicitly rather than
relying on what happens to be in the container's environment.
"""

from __future__ import annotations

import pytest

from tailorcraft.infrastructure.settings import Environment, MisconfiguredSettings, Settings
from tailorcraft.infrastructure.tasks import app as tasks_app_module
from tailorcraft.infrastructure.tasks.app import TASK_TIME_LIMIT_SECONDS


def test_production_with_an_empty_key_refuses_to_boot() -> None:
    with pytest.raises(MisconfiguredSettings):
        Settings(app_env="production", gemini_api_key="")


def test_production_with_a_whitespace_only_key_refuses_to_boot() -> None:
    """A whitespace-only key counts as empty (the class's own docstring: "we have a key" must not be
    satisfiable by a space) — the plausible hand-edit-a-`.env` typo, not a hypothetical."""
    with pytest.raises(MisconfiguredSettings):
        Settings(app_env="production", gemini_api_key="   ")


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_dev_and_test_boot_keyless(app_env: Environment) -> None:
    """The permission is the point, not a leniency: it is what lets the whole suite run with no key
    and no possibility of a key that appeared in CI silently starting to spend money."""
    settings = Settings(app_env=app_env, gemini_api_key="")

    assert settings.gemini_api_key == ""


def test_the_guard_never_leaks_a_secret_from_a_sibling_field() -> None:
    """`MisconfiguredSettings` rather than a bare `ValueError`, and this is the property that makes
    the distinction load-bearing rather than stylistic. Pydantic wraps a `ValueError` raised inside a
    validator into a `ValidationError` whose rendered message carries `input_value` — the entire
    settings input dict, including `database_url`'s password. `MisconfiguredSettings` is not a
    `ValueError` (nor an `AssertionError`), so pydantic lets it propagate untouched, carrying only the
    sentence it was given."""
    sentinel_password = "sentinel-password-must-never-appear-in-a-crash-log-Zx9Q"

    with pytest.raises(MisconfiguredSettings) as exc_info:
        Settings(
            app_env="production",
            gemini_api_key="",
            database_url=(
                f"postgresql+asyncpg://tailorcraft:{sentinel_password}@postgres:5432/tailorcraft"
            ),
        )

    assert sentinel_password not in str(exc_info.value)


# --- D3t (AC-20/AC-21): the export stale-window guard, and the five ordered numbers around it ---
#
# `create_celery`'s second startup guard lives in `infrastructure/tasks/app.py`, not here — but D3t
# places its tests beside `Settings`' own rather than in `tests/integration/tasks/test_celery_config.py`
# (where 1.3's sibling guard is tested), so the boundary tests below mirror that file's
# `test_create_celery_refuses_a_stale_window_exactly_equal_to_the_hard_time_limit` /
# `..._accepts_a_stale_window_one_second_above_...` pair — same seam-patching technique
# (`monkeypatch.setattr(tasks_app_module, "get_settings", ...)`, since `create_celery` takes no
# argument and reads `get_settings()` itself), same reasoning, `export_*` in place of `tailoring_*`.
# No broker, no database: `create_celery()` only builds the `Celery` object and reads its own
# `.conf`, exactly as that file's docstring establishes.


def test_create_celery_refuses_an_export_stale_window_exactly_equal_to_the_hard_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary, tested at the boundary rather than far from it. 180 is
    `TASK_TIME_LIMIT_SECONDS`, and the guard is `<=`: a test that only tried a value far below 180
    (say, 60) would still pass against a bug that used `<` instead of `<=` and let 180 itself
    through — the exact instant a live render killed by the hard limit and a sweep tick judging the
    same job stale would coincide (`tasks/app.py`'s own comment on this guard). Equality must still
    be refused.
    """
    settings = Settings(app_env="test", export_stale_after_seconds=TASK_TIME_LIMIT_SECONDS)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)

    with pytest.raises(MisconfiguredSettings) as exc_info:
        tasks_app_module.create_celery()

    message = str(exc_info.value)
    assert "export_stale_after_seconds" in message
    assert "task_time_limit" in message


def test_create_celery_accepts_an_export_stale_window_one_second_above_the_hard_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same boundary: `TASK_TIME_LIMIT_SECONDS + 1` is the smallest value that
    must be ACCEPTED. Stated here, in the same commit as the refusing side, so the boundary is
    pinned from both directions rather than only from the one that raises."""
    settings = Settings(app_env="test", export_stale_after_seconds=TASK_TIME_LIMIT_SECONDS + 1)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)

    built = tasks_app_module.create_celery()

    assert built.conf.task_time_limit == TASK_TIME_LIMIT_SECONDS


def test_export_timeout_defaults_are_ordered_inline_through_stale_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-21 names five numbers that must stay ordered: `export_inline_timeout_seconds` (5) <
    `export_render_timeout_seconds` (60) < the Celery soft limit (120) < the hard limit (180) <
    `export_stale_after_seconds` (300). The assertion is a single chained comparison read off the
    real objects — `Settings`' own field defaults for the two `export_*` timeouts and the two
    Celery-side numbers off the `Celery` app `create_celery` actually builds — never a re-typed
    `5 < 60 < 120 < 180 < 300`. Restating the literals would only echo the defaults back at
    themselves and could never fail: this test fails the moment any ONE of the five is changed
    without the others moving to match, because it is the relationship between the live values that
    is asserted, not their current numbers.
    """
    inline_default = Settings.model_fields["export_inline_timeout_seconds"].default
    render_default = Settings.model_fields["export_render_timeout_seconds"].default
    stale_default = Settings.model_fields["export_stale_after_seconds"].default
    settings = Settings(app_env="test", export_stale_after_seconds=stale_default)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)

    built = tasks_app_module.create_celery()

    assert (
        inline_default
        < render_default
        < built.conf.task_soft_time_limit
        < built.conf.task_time_limit
        < stale_default
    )
