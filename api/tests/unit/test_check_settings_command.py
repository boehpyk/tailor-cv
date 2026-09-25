"""`tailorcraft.cli check-settings` (T33, T46, OQ-2) — every startup refusal the API process has,
asked once, in one process, before uvicorn is exec'd.

**The seam: a `get_settings` provider, monkeypatched onto `check_settings_command`, never real
environment variables.** `run_from_cli` calls `get_settings.cache_clear()` before its `try:` block —
outside the range any `except` in that function can reach — so a replacement must expose a
`cache_clear` a bare `lambda` does not have; `functools.lru_cache` around a small function supplies
one for free and is the whole trick. This also sidesteps two things a real-environment version of
this file would have to fight: whatever `.env` file happens to be readable from this process's cwd
(there is none under `/app` in this container, but the point is not to depend on that), and leaving
the *real*, module-level `get_settings()` cache dirty for every test that runs after this file in the
same session — nothing here ever touches it. `Settings(...)` is built the same way
`tests/unit/test_settings.py` already builds it for its own refusal tests: explicit keyword arguments,
so the object under test is exactly what the assertion names and nothing an ambient `.env` supplied.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import lru_cache
from typing import Final

import pytest
from pydantic import SecretStr

from tailorcraft.infrastructure import check_settings_command
from tailorcraft.infrastructure.settings import (
    JWT_SIGNING_KEY_MIN_BYTES,
    JWT_SIGNING_KEY_PLACEHOLDER,
    Settings,
)

_TASK_TIME_LIMIT_SECONDS: Final = 180  # `infrastructure/tasks/limits.py`'s own constant, restated
# here only as a boundary value to build test settings from — never re-derived as a rule.

_A_STRONG_PRODUCTION_KEY: Final = "a" * (JWT_SIGNING_KEY_MIN_BYTES + 8)


def _provider(build: Callable[[], Settings]) -> Callable[[], Settings]:
    """A `get_settings`-shaped replacement: calling it runs `build()`, and it carries a working
    `cache_clear` (an `lru_cache`'d wrapper gets one for free) so `run_from_cli`'s unconditional
    `get_settings.cache_clear()` — called before any `try:` — does not itself raise."""
    return lru_cache(maxsize=1)(build)


@pytest.fixture(autouse=True)
def _reset_the_real_cache() -> Iterator[None]:
    """Every test in this file monkeypatches the module's `get_settings` reference, never the real,
    process-wide one — but clear it anyway, before and after, so a test elsewhere in the session that
    calls the real `get_settings()` never observes a state this file might have left behind by
    accident."""
    from tailorcraft.infrastructure.settings import get_settings as real_get_settings

    real_get_settings.cache_clear()
    yield
    real_get_settings.cache_clear()


# --- settings ok -----------------------------------------------------------------------------


def test_good_settings_exit_0_and_print_settings_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_settings_command, "get_settings", _provider(lambda: Settings(app_env="test"))
    )

    exit_code = check_settings_command.run_from_cli()

    assert exit_code == check_settings_command.EXIT_OK
    out = capsys.readouterr().out
    assert out.strip() == "settings ok"


# --- MisconfiguredSettings: the sentence, never a value -----------------------------------------


def test_missing_gemini_key_in_production_exits_1_with_the_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_settings_command,
        "get_settings",
        _provider(
            lambda: Settings(
                app_env="production",
                gemini_api_key="",
                jwt_signing_key=SecretStr(_A_STRONG_PRODUCTION_KEY),
            )
        ),
    )

    exit_code = check_settings_command.run_from_cli()

    assert exit_code == check_settings_command.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "check-settings:" in err
    assert "GEMINI_API_KEY" in err


def test_placeholder_jwt_signing_key_in_production_exits_1_with_the_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_settings_command,
        "get_settings",
        _provider(
            lambda: Settings(
                app_env="production",
                gemini_api_key="a-real-key",
                jwt_signing_key=SecretStr(JWT_SIGNING_KEY_PLACEHOLDER),
            )
        ),
    )

    exit_code = check_settings_command.run_from_cli()

    assert exit_code == check_settings_command.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "JWT_SIGNING_KEY" in err
    assert "placeholder" in err
    # The placeholder itself is 33 ASCII bytes — short enough that its accidental presence in the
    # message would be trivial to miss by eye. Checked anyway, literally.
    assert JWT_SIGNING_KEY_PLACEHOLDER not in err


def test_short_jwt_signing_key_in_production_exits_1_with_the_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_settings_command,
        "get_settings",
        _provider(
            lambda: Settings(
                app_env="production",
                gemini_api_key="a-real-key",
                jwt_signing_key=SecretStr("too-short"),
            )
        ),
    )

    exit_code = check_settings_command.run_from_cli()

    assert exit_code == check_settings_command.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "JWT_SIGNING_KEY" in err
    assert "shorter than" in err
    assert "too-short" not in err


def test_tailoring_stale_window_at_the_hard_limit_exits_1_with_the_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stale-window refusals apply in every environment, not only production (`tasks/limits.py`'s
    own docstring) — `app_env="test"` here is deliberate, to prove that."""
    monkeypatch.setattr(
        check_settings_command,
        "get_settings",
        _provider(
            lambda: Settings(app_env="test", tailoring_stale_after_seconds=_TASK_TIME_LIMIT_SECONDS)
        ),
    )

    exit_code = check_settings_command.run_from_cli()

    assert exit_code == check_settings_command.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "TAILORING_STALE_AFTER_SECONDS" in err
    assert str(_TASK_TIME_LIMIT_SECONDS) in err


def test_export_stale_window_at_the_hard_limit_exits_1_with_the_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_settings_command,
        "get_settings",
        _provider(
            lambda: Settings(app_env="test", export_stale_after_seconds=_TASK_TIME_LIMIT_SECONDS)
        ),
    )

    exit_code = check_settings_command.run_from_cli()

    assert exit_code == check_settings_command.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "EXPORT_STALE_AFTER_SECONDS" in err
    assert str(_TASK_TIME_LIMIT_SECONDS) in err


def test_test_redis_url_collision_exits_1_with_the_sentence_and_never_a_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_isolate_the_test_redis_database` also runs unconditionally — a `TEST_REDIS_URL` pointed at
    the same store as `REDIS_URL` must refuse regardless of environment, and its message names
    database numbers, never a URL (`settings.py`'s own docstring). The password planted here proves
    the negative rather than assuming it."""
    secret_marker = "sentinel-redis-password-must-never-appear-in-a-crash-log-Zx9Q"
    redis_url = f"redis://:{secret_marker}@redis:6379/0"
    monkeypatch.setattr(
        check_settings_command,
        "get_settings",
        _provider(lambda: Settings(app_env="test", redis_url=redis_url, test_redis_url=redis_url)),
    )

    exit_code = check_settings_command.run_from_cli()

    assert exit_code == check_settings_command.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "TEST_REDIS_URL" in err
    assert secret_marker not in err


# --- pydantic.ValidationError: field names and types only, never an input value ------------------


def test_a_validation_error_prints_field_names_and_types_never_an_input_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A field that fails pydantic's own type coercion — not one of the four `MisconfiguredSettings`
    refusals above — takes the `except ValidationError` branch. `access_token_ttl_minutes` gets a
    string pydantic cannot parse as an int (a planted marker standing in for a value that should
    never reach the printed message), and `database_url` carries a second, independent secret in the
    very same settings snapshot — proving the leak-free property holds for the *whole* object, not
    only for the field that actually failed.
    """
    bad_int_marker = "NOT-AN-INT-MARKER"
    secret_in_database_url = "sentinel-db-password-must-never-appear-in-a-crash-log-Qw7Y"

    def _build() -> Settings:
        # `model_validate`, not `Settings(...)`: the marker is deliberately the wrong TYPE for this
        # field (a string where an int must parse), so passing it as a keyword argument is itself a
        # static type error `Settings(...)`'s normal constructor would refuse mypy --strict on. This
        # is `BaseSettings`' equivalent of the untyped input a malformed `.env` value actually is.
        return Settings.model_validate(
            {
                "app_env": "test",
                "access_token_ttl_minutes": bad_int_marker,
                "database_url": (
                    f"postgresql+asyncpg://tailorcraft:{secret_in_database_url}@postgres:5432/"
                    "tailorcraft_test"
                ),
            }
        )

    monkeypatch.setattr(check_settings_command, "get_settings", _provider(_build))

    exit_code = check_settings_command.run_from_cli()

    assert exit_code == check_settings_command.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "ACCESS_TOKEN_TTL_MINUTES" in err
    assert "int_parsing" in err
    assert bad_int_marker not in err
    assert secret_in_database_url not in err
    assert "postgres" not in err  # no fragment of the DSN at all, not just the password


# --- anything else: the exception's type only ---------------------------------------------------


def test_an_unexpected_exception_prints_only_its_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message_marker = "a message that must never reach the terminal"

    def _blow_up() -> Settings:
        raise KeyError(message_marker)

    monkeypatch.setattr(check_settings_command, "get_settings", _provider(_blow_up))

    exit_code = check_settings_command.run_from_cli()

    assert exit_code == check_settings_command.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "KeyError" in err
    assert message_marker not in err
