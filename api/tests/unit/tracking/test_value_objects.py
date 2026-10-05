"""AC-1 and AC-2: `ApplicationStage` is closed and ordered; `ApplicationTitle` trims and refuses.

Pure domain tests: no I/O, no fixtures, no event loop, no mocks.

`ApplicationTitle.__post_init__` is a deliberate no-op in the T3 skeleton, so every "refused" test
here goes red on `DID NOT RAISE`, and every "trims" test on an `AssertionError` -- never on the
skeleton's own `NotImplementedError` (which subclasses `RuntimeError`).

The stage tests are green on arrival: `ApplicationStage` is complete in the skeleton and the tests
are regression guards for it (reordering the members reorders the board).
"""

from __future__ import annotations

import dataclasses

import pytest

from tailorcraft.domain.tracking.errors import InvalidApplicationTitle
from tailorcraft.domain.tracking.value_objects import ApplicationStage, ApplicationTitle

# ---- AC-1 -------------------------------------------------------------------------------------


def test_application_stage_is_exactly_six_values_in_board_order() -> None:
    assert [stage.value for stage in ApplicationStage] == [
        "to_apply",
        "applied",
        "interviewing",
        "offer",
        "rejected",
        "withdrawn",
    ]


def test_application_stage_is_a_string_enum_so_it_serialises_as_its_wire_value() -> None:
    assert isinstance(ApplicationStage.OFFER, str)
    assert str(ApplicationStage.OFFER) == "offer"


def test_application_stage_refuses_a_value_outside_the_six() -> None:
    with pytest.raises(ValueError, match="ghosted"):
        ApplicationStage("ghosted")


# ---- AC-2: what a title accepts ---------------------------------------------------------------


def test_a_title_is_trimmed_of_surrounding_whitespace() -> None:
    assert ApplicationTitle("  Staff Engineer at Acme \n").value == "Staff Engineer at Acme"


def test_a_title_with_no_surrounding_whitespace_is_kept_as_it_is() -> None:
    assert ApplicationTitle("Staff Engineer").value == "Staff Engineer"


def test_a_title_keeps_its_interior_spaces() -> None:
    assert ApplicationTitle("Staff   Engineer").value == "Staff   Engineer"


def test_a_title_of_exactly_120_characters_is_accepted() -> None:
    assert ApplicationTitle("a" * 120).value == "a" * 120


def test_the_length_limit_counts_after_trimming() -> None:
    assert ApplicationTitle("  " + "a" * 120 + "  ").value == "a" * 120


def test_max_length_is_120() -> None:
    assert ApplicationTitle.MAX_LENGTH == 120


def test_a_title_may_hold_non_ascii_text() -> None:
    assert ApplicationTitle("Ingénieur logiciel — Zürich").value == "Ingénieur logiciel — Zürich"


def test_the_length_limit_counts_characters_not_bytes() -> None:
    assert ApplicationTitle("é" * 120).value == "é" * 120


# ---- AC-2: what a title refuses ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["", " ", "   ", "\t \n"], ids=["empty", "space", "spaces", "ws"])
def test_a_title_that_is_empty_after_trimming_is_refused(text: str) -> None:
    with pytest.raises(InvalidApplicationTitle):
        ApplicationTitle(text)


def test_a_title_of_121_characters_is_refused() -> None:
    with pytest.raises(InvalidApplicationTitle):
        ApplicationTitle("a" * 121)


# One interior character from each region of category Cc: C0 (NUL, TAB, LF, CR, ESC, US), DEL, and
# C1 (the first and last). Interior, so that trimming cannot remove it before the check sees it.
_CONTROL_CHARACTERS = {
    "NUL": "\x00",
    "TAB": "\t",
    "LF": "\n",
    "CR": "\r",
    "ESC": "\x1b",
    "US": "\x1f",
    "DEL": "\x7f",
    "PAD": "\x80",
    "APC": "\x9f",
}


@pytest.mark.parametrize("char", list(_CONTROL_CHARACTERS.values()), ids=list(_CONTROL_CHARACTERS))
def test_a_title_with_an_interior_control_character_is_refused(char: str) -> None:
    with pytest.raises(InvalidApplicationTitle):
        ApplicationTitle(f"Staff{char}Engineer")


def test_an_ordinary_space_is_not_a_control_character() -> None:
    # The discriminating positive for the control-character rule: it must not over-refuse.
    assert ApplicationTitle("Staff Engineer").value == "Staff Engineer"


@pytest.mark.parametrize(
    "bad",
    ["SECRETMARKER" + "x" * 130, "SECRETMARKER\x00tail", "SECRETMARKER\tTAB"],
    ids=["too_long", "nul", "tab"],
)
def test_the_refusal_message_never_quotes_the_title(bad: str) -> None:
    with pytest.raises(InvalidApplicationTitle) as excinfo:
        ApplicationTitle(bad)

    assert "SECRETMARKER" not in str(excinfo.value)
    assert "SECRETMARKER" not in repr(excinfo.value)
    assert str(excinfo.value) != ""


# ---- AC-2: value semantics --------------------------------------------------------------------


def test_titles_with_the_same_trimmed_text_are_equal() -> None:
    assert ApplicationTitle("  Acme ") == ApplicationTitle("Acme")


def test_titles_with_different_text_are_not_equal() -> None:
    assert ApplicationTitle("Acme") != ApplicationTitle("Globex")


def test_equal_titles_hash_alike() -> None:
    assert hash(ApplicationTitle(" Acme")) == hash(ApplicationTitle("Acme"))


def test_a_title_is_frozen() -> None:
    title = ApplicationTitle("Acme")

    with pytest.raises(dataclasses.FrozenInstanceError):
        title.value = "Other"  # type: ignore[misc]


def test_a_title_is_slotted() -> None:
    title = ApplicationTitle("Acme")

    assert not hasattr(title, "__dict__")
