"""Unit tests for `Settings`' own validation (AC-32/G-32's boot guard), and for the reason
`MisconfiguredSettings` exists rather than a bare `ValueError` (see that class's own docstring).

Pure: no database, no event loop, no fixtures beyond what `Settings(...)` itself needs. `Settings()`
reads `.env` by default, so every test below passes every field it cares about explicitly rather than
relying on what happens to be in the container's environment.
"""

from __future__ import annotations

import pytest

from tailorcraft.infrastructure.settings import Environment, MisconfiguredSettings, Settings


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
