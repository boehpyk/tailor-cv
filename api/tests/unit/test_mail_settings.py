"""AC-24 (slice 2.5, T20 RED): the five new production refusals, the bounds beside them, and their
acceptance outside production — asserted through `Settings(...)` and through `check-settings`.

Source of truth: AC-24 and technical plan §3's Settings table, never the code. Each refusal is a
`MisconfiguredSettings` sentence naming the variable and never a value; the `check-settings` half
asserts exit 1 and that a planted marker, put in the field that is refused *and* in the neighbouring
secret, appears nowhere on stderr.

Seam for `check-settings`: a `get_settings` replacement, exactly as `test_check_settings_command.py`
does (its module docstring says why). The baseline is `PRODUCTION_MAIL`, so each test changes one
thing from a configuration that boots.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any, Final

import pytest
from pydantic import SecretStr, ValidationError

from tailorcraft.infrastructure import check_settings_command
from tailorcraft.infrastructure.settings import (
    JWT_SIGNING_KEY_MIN_BYTES,
    Environment,
    MisconfiguredSettings,
    Settings,
)
from tests.unit._production_mail import PRODUCTION_MAIL

_KEY: Final = SecretStr("k" * (JWT_SIGNING_KEY_MIN_BYTES + 8))
_MARKER: Final = "MARKER-must-never-be-printed-7Qx2"


def _production(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "production",
        "gemini_api_key": "a-real-key",
        "jwt_signing_key": _KEY,
        **PRODUCTION_MAIL,
    }
    return Settings(**{**base, **overrides})


def _non_production(**overrides: Any) -> Settings:
    return Settings(**{"app_env": "test", **overrides})


def _provider(build: Callable[[], Settings]) -> Callable[[], Settings]:
    return lru_cache(maxsize=1)(build)


def _check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    build: Callable[[], Settings],
) -> tuple[int, str]:
    monkeypatch.setattr(check_settings_command, "get_settings", _provider(build))
    code = check_settings_command.run_from_cli()
    return code, capsys.readouterr().err


# The refusals: (id, overrides that break exactly one rule, the variable the sentence must name).
_REFUSALS: Final = [
    pytest.param({"mail_smtp_host": ""}, "MAIL_SMTP_HOST", id="6-empty-host"),
    pytest.param({"mail_smtp_host": "   "}, "MAIL_SMTP_HOST", id="6-blank-host"),
    pytest.param({"mail_smtp_security": "none"}, "MAIL_SMTP_SECURITY", id="7-security-none"),
    pytest.param({"mail_smtp_username": ""}, "MAIL_SMTP_USERNAME", id="8-empty-username"),
    pytest.param(
        {"mail_smtp_password": SecretStr("")}, "MAIL_SMTP_PASSWORD", id="8-empty-password"
    ),
    pytest.param({"mail_from_address": ""}, "MAIL_FROM_ADDRESS", id="9-empty-sender"),
    pytest.param(
        {"mail_from_address": f"{_MARKER}-not-an-address"},
        "MAIL_FROM_ADDRESS",
        id="9-unparseable-sender",
    ),
    pytest.param(
        {"public_base_url": f"http://{_MARKER}.example"}, "PUBLIC_BASE_URL", id="10-http-base-url"
    ),
    pytest.param({"public_base_url": "cv.example"}, "PUBLIC_BASE_URL", id="10-schemeless-base-url"),
]


def test_a_valid_production_mail_configuration_boots() -> None:
    """The control: the baseline every refusal below departs from by exactly one field."""
    settings = _production()

    assert settings.mail_smtp_host == PRODUCTION_MAIL["mail_smtp_host"]


@pytest.mark.parametrize(("overrides", "variable"), _REFUSALS)
def test_production_refuses_a_bad_mail_setting_naming_the_variable(
    overrides: dict[str, Any], variable: str
) -> None:
    with pytest.raises(MisconfiguredSettings) as exc_info:
        _production(**overrides)

    assert variable in str(exc_info.value)


@pytest.mark.parametrize(("overrides", "variable"), _REFUSALS)
def test_the_refusal_sentence_never_contains_the_refused_value_or_the_password(
    overrides: dict[str, Any], variable: str
) -> None:
    secret = "smtp-password-marker-Zk39"
    with pytest.raises(MisconfiguredSettings) as exc_info:
        _production(**{"mail_smtp_password": SecretStr(secret), **overrides})

    sentence = str(exc_info.value)
    assert _MARKER not in sentence
    assert secret not in sentence
    assert "http://" not in sentence


@pytest.mark.parametrize(("overrides", "variable"), _REFUSALS)
def test_check_settings_exits_1_with_the_sentence_and_prints_no_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, Any],
    variable: str,
) -> None:
    secret = "smtp-password-marker-Zk39"
    code, err = _check(
        monkeypatch,
        capsys,
        lambda: _production(**{"mail_smtp_password": SecretStr(secret), **overrides}),
    )

    assert code == check_settings_command.EXIT_REFUSED
    assert "check-settings:" in err
    assert variable in err
    assert _MARKER not in err
    assert secret not in err


def test_check_settings_exits_0_for_a_valid_production_mail_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _err = _check(monkeypatch, capsys, _production)

    assert code == check_settings_command.EXIT_OK


# --- dev and test accept all five ------------------------------------------------------------


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_dev_and_test_accept_every_refused_mail_value(app_env: Environment) -> None:
    settings = Settings(
        app_env=app_env,
        mail_smtp_host="",
        mail_smtp_security="none",
        mail_smtp_username="",
        mail_smtp_password=SecretStr(""),
        mail_from_address="",
        public_base_url="http://localhost:8080",
    )

    assert settings.mail_smtp_security == "none"


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_check_settings_prints_settings_ok_for_dev_and_test_mail_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], app_env: Environment
) -> None:
    code, _err = _check(
        monkeypatch,
        capsys,
        lambda: Settings(app_env=app_env, mail_smtp_security="none", mail_smtp_host="mailpit"),
    )

    assert code == check_settings_command.EXIT_OK


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_production_accepts_starttls_and_implicit_tls(security: str) -> None:
    settings = _production(mail_smtp_security=security)

    assert settings.mail_smtp_security == security


# --- the existing five still refuse, beside the new ones -------------------------------------


def test_the_gemini_refusal_still_fires_with_valid_mail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = _check(monkeypatch, capsys, lambda: _production(gemini_api_key=""))

    assert code == check_settings_command.EXIT_REFUSED
    assert "GEMINI_API_KEY" in err


# --- plan §3 bounds --------------------------------------------------------------------------

_BOUNDS: Final = [
    ("mail_smtp_port", 0, 1),
    ("mail_smtp_port", 65536, 65535),
    ("mail_send_timeout_seconds", 0, 1),
    ("mail_send_timeout_seconds", 31, 30),
    ("mail_total_deadline_seconds", 4, 5),
    ("mail_total_deadline_seconds", 121, 119),
    ("mail_max_attempts", 0, 1),
    ("mail_max_attempts", 4, 3),
    ("email_confirmation_ttl_hours", 0, 1),
    ("email_confirmation_ttl_hours", 73, 72),
    ("password_reset_ttl_minutes", 9, 10),
    ("password_reset_ttl_minutes", 241, 240),
    ("register_rate_limit_per_email_per_hour", 0, 1),
    ("password_reset_rate_limit_per_ip_per_hour", 0, 1),
    ("password_reset_rate_limit_per_email_per_hour", 0, 1),
]


@pytest.mark.parametrize(("field", "bad", "good"), _BOUNDS)
def test_a_setting_outside_its_bound_refuses_to_boot(field: str, bad: int, good: int) -> None:
    with pytest.raises(ValidationError):
        _non_production(**{field: bad})


@pytest.mark.parametrize(("field", "bad", "good"), _BOUNDS)
def test_a_setting_at_its_bound_boots(field: str, bad: int, good: int) -> None:
    settings = _non_production(**{field: good})

    assert getattr(settings, field) == good


def test_the_documented_defaults() -> None:
    settings = Settings(app_env="test")

    expected = {
        "email_confirmation_ttl_hours": 24,
        "password_reset_ttl_minutes": 60,
        "register_rate_limit_per_email_per_hour": 3,
        "password_reset_rate_limit_per_ip_per_hour": 10,
        "password_reset_rate_limit_per_email_per_hour": 3,
    }
    actual = {name: getattr(settings, name, None) for name in expected}

    assert actual == expected


# --- the deadline must sit below Celery's soft limit (120 s), in every environment ------------


def test_a_mail_deadline_equal_to_the_celery_soft_limit_exits_1_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """120 is inside the 5-120 field bound, so only the "below the soft limit" rule can refuse it."""
    code, err = _check(
        monkeypatch, capsys, lambda: Settings(app_env="test", mail_total_deadline_seconds=120)
    )

    assert code == check_settings_command.EXIT_REFUSED
    assert "MAIL_TOTAL_DEADLINE_SECONDS" in err


def test_a_mail_deadline_one_second_below_the_celery_soft_limit_is_accepted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _err = _check(
        monkeypatch, capsys, lambda: Settings(app_env="test", mail_total_deadline_seconds=119)
    )

    assert code == check_settings_command.EXIT_OK
