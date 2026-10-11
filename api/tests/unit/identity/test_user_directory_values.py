"""Slice 4.2 (ADR-0033), the directory's value objects: AC-4 (`EmailSearch`) and AC-5
(`UserDirectoryCursor`).

Pure domain tests. Against the T2 skeleton `EmailSearch.parse` raises `NotImplementedError` and the
constructors' `__post_init__` is a no-op, so the "refused" tests go red on `DID NOT RAISE`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from tailorcraft.domain.identity.errors import InvalidEmailSearch, InvalidUserDirectoryCursor
from tailorcraft.domain.identity.user_directory import EmailSearch, UserDirectoryCursor
from tailorcraft.domain.identity.value_objects import UserId

_USER_ID = UserId(value=UUID("66666666-6666-7666-8666-666666666666"))
_WHEN = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)


def _parsed(raw: str) -> EmailSearch:
    result = EmailSearch.parse(raw)
    assert result is not None
    return result


# --- AC-4 ------------------------------------------------------------------------------------


def test_parse_strips_and_lower_cases() -> None:
    assert _parsed("  Alex@Example.COM \n").value == "alex@example.com"


@pytest.mark.parametrize("raw", ["", " ", "   ", "\t \n"])
def test_parse_of_empty_or_whitespace_is_no_search(raw: str) -> None:
    assert EmailSearch.parse(raw) is None


def test_parse_accepts_254_characters_after_stripping() -> None:
    assert _parsed(" " + "a" * 254 + " ").value == "a" * 254


def test_parse_refuses_255_characters() -> None:
    with pytest.raises(InvalidEmailSearch):
        EmailSearch.parse("a" * 255)


@pytest.mark.parametrize("control", ["\x00", "\x07", "\x1b", "\x7f", "\x85"])
def test_parse_refuses_a_control_character_inside(control: str) -> None:
    with pytest.raises(InvalidEmailSearch):
        EmailSearch.parse(f"al{control}ex")


def test_parse_refuses_an_interior_newline() -> None:
    with pytest.raises(InvalidEmailSearch):
        EmailSearch.parse("al\nex")


@pytest.mark.parametrize("raw", ["%", "_", "\\", "a%b_c\\d", "50%_off"])
def test_pattern_characters_are_accepted_as_literals(raw: str) -> None:
    assert _parsed(raw).value == raw


def test_the_refusal_never_carries_the_input() -> None:
    marker = "SECRET-MARKER-" + "x" * 255
    with pytest.raises(InvalidEmailSearch) as too_long:
        EmailSearch.parse(marker)
    assert "SECRET-MARKER" not in str(too_long.value)
    assert "SECRET-MARKER" not in repr(too_long.value)
    with pytest.raises(InvalidEmailSearch) as control:
        EmailSearch.parse("SECRET-MARKER\x00")
    assert "SECRET-MARKER" not in str(control.value)
    assert "SECRET-MARKER" not in repr(control.value)


def test_constructing_directly_enforces_the_same_rules() -> None:
    with pytest.raises(InvalidEmailSearch):
        EmailSearch("a" * 255)


# --- AC-5 ------------------------------------------------------------------------------------


def test_a_cursor_accepts_an_aware_whole_second_time() -> None:
    cursor = UserDirectoryCursor(created_at=_WHEN, user_id=_USER_ID)
    assert cursor.created_at == _WHEN
    assert cursor.user_id == _USER_ID


def test_a_cursor_accepts_a_non_utc_aware_time() -> None:
    plus_two = timezone(timedelta(hours=2))
    UserDirectoryCursor(
        created_at=datetime(2026, 10, 1, 12, 0, 0, tzinfo=plus_two), user_id=_USER_ID
    )


def test_a_cursor_refuses_a_naive_time() -> None:
    with pytest.raises(InvalidUserDirectoryCursor):
        UserDirectoryCursor(created_at=datetime(2026, 10, 1, 10, 0, 0), user_id=_USER_ID)


def test_a_cursor_refuses_a_sub_second_time() -> None:
    with pytest.raises(InvalidUserDirectoryCursor):
        UserDirectoryCursor(created_at=_WHEN.replace(microsecond=1), user_id=_USER_ID)


def test_two_cursors_with_the_same_fields_are_equal() -> None:
    assert UserDirectoryCursor(_WHEN, _USER_ID) == UserDirectoryCursor(_WHEN, _USER_ID)
