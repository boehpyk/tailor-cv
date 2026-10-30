"""T6 hardening: the edges of the `scoring` value objects that T5's RED left soft. Pure: no I/O.

Each case comes from the grammar in the spec (AC-25) and the value objects' docstrings, not from
running the code and recording the answer."""

from __future__ import annotations

from typing import Any

import pytest

from tailorcraft.domain.scoring.errors import (
    InvalidFitGap,
    InvalidMicroUsd,
    InvalidModelName,
    InvalidPromptVersion,
    InvalidScoringUsage,
)
from tailorcraft.domain.scoring.value_objects import (
    FitGap,
    MicroUsd,
    ModelName,
    PromptVersion,
    ScoringUsage,
)

# --- ModelName ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "m" * 65,
        "gemini 2",
        " gemini",
        "gemini\n",
        "\t",
        chr(0xA0),
        "gem\x00ini",
        "gem\x1fini",
        "gem\x7fini",
        "gem\x80ini",
        "gem\x9fini",
    ],
    ids=[
        "empty",
        "65_chars",
        "inner_space",
        "leading_space",
        "trailing_newline",
        "tab",
        "nbsp",
        "nul",
        "c0_unit_separator",
        "del",
        "c1_start",
        "c1_end",
    ],
)
def test_a_malformed_model_name_is_refused(bad: str) -> None:
    with pytest.raises(InvalidModelName):
        ModelName(bad)


def test_a_64_character_model_name_is_accepted() -> None:
    assert ModelName("m" * 64).value == "m" * 64


# --- PromptVersion ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "v" * 17, "v 1", "v/1", "v1é", "v1\n", "\nv1", "v1 "],
    ids=[
        "empty",
        "17_chars",
        "space",
        "slash",
        "non_ascii",
        "trailing_newline",
        "leading_newline",
        "trailing_space",
    ],
)
def test_a_malformed_prompt_version_is_refused(bad: str) -> None:
    with pytest.raises(InvalidPromptVersion):
        PromptVersion(bad)


@pytest.mark.parametrize("good", ["1", "v1.2-rc_3", "A" * 16])
def test_a_valid_prompt_version_is_accepted(good: str) -> None:
    assert PromptVersion(good).value == good


# --- ScoringUsage -------------------------------------------------------------------------------

_COUNTS = ("input_tokens", "output_tokens", "unmetered_attempts", "duration_ms")


def _usage(**override: Any) -> ScoringUsage:
    fields: dict[str, Any] = {
        "input_tokens": 1,
        "output_tokens": 1,
        "unmetered_attempts": 0,
        "duration_ms": 1,
        "model": ModelName("gemini-x"),
        "prompt_version": PromptVersion("1"),
    }
    fields.update(override)
    return ScoringUsage(**fields)


@pytest.mark.parametrize("field", _COUNTS)
def test_a_negative_usage_count_is_refused(field: str) -> None:
    with pytest.raises(InvalidScoringUsage):
        _usage(**{field: -1})


@pytest.mark.parametrize("field", _COUNTS)
def test_a_bool_usage_count_is_refused(field: str) -> None:
    with pytest.raises(InvalidScoringUsage):
        _usage(**{field: True})


@pytest.mark.parametrize("field", _COUNTS)
@pytest.mark.parametrize("bad", [1.0, "1", None], ids=["float", "str", "none"])
def test_a_non_int_usage_count_is_refused(field: str, bad: object) -> None:
    with pytest.raises(InvalidScoringUsage):
        _usage(**{field: bad})


# --- FitGap -------------------------------------------------------------------------------------


def test_a_tab_inside_a_gap_collapses_to_a_space() -> None:
    assert FitGap("Kubernetes\texperience").value == "Kubernetes experience"


@pytest.mark.parametrize(
    "bad",
    [
        "Kubernetes\x0bexperience",
        "Kubernetes\x0cexperience",
        "Kubernetes\x1cexperience",
        "Kubernetes" + chr(0x2028) + "experience",
        "Kubernetes" + chr(0x2029) + "experience",
        "Kubernetes experience\n",
    ],
    ids=["vt", "ff", "fs", "u2028", "u2029", "trailing_newline"],
)
def test_a_gap_with_a_line_break_or_control_character_is_refused(bad: str) -> None:
    with pytest.raises(InvalidFitGap):
        FitGap(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "see ftp://files.example/x",
        "see WWW.example.com",
        "see www.example.com",
        "id ٠١٢٣٤٥٦",
        "id 1234567",
    ],
    ids=["ftp_scheme", "uppercase_www", "lowercase_www", "non_ascii_digits", "ascii_digits"],
)
def test_a_gap_holding_a_url_or_seven_digits_is_refused(bad: str) -> None:
    with pytest.raises(InvalidFitGap):
        FitGap(bad)


def test_a_gap_with_an_at_sign_but_no_email_is_accepted() -> None:
    assert FitGap("C@scale").value == "C@scale"


# --- MicroUsd -----------------------------------------------------------------------------------


def test_multiplying_money_by_a_negative_count_is_refused() -> None:
    with pytest.raises(InvalidMicroUsd):
        MicroUsd(5) * -1
