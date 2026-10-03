"""AC-1: `OneTimeToken` — grammar, masking, `reveal()`; AC-6: the no-op skeleton's recorded red.

Pure domain tests. Refusals go red on `Failed: DID NOT RAISE` (the skeleton's `__post_init__` is a
no-op on purpose). The masking assertions are **exact equality** with `OneTimeToken(***)`: the
skeleton's generated `repr` prints `OneTimeToken()`, so an "absence of the value" assertion would
pass vacuously against it.
"""

from __future__ import annotations

import dataclasses
import secrets

import pytest

from tailorcraft.domain.identity.errors import InvalidOneTimeToken
from tailorcraft.domain.identity.value_objects import OneTimeToken

_VALID = "A" * 20 + "b" * 10 + "3" * 5 + "_-_-_-_-"  # 43 characters, every class of the alphabet
_MASKED = "OneTimeToken(***)"


def test_the_fixture_is_43_characters() -> None:
    assert len(_VALID) == 43


def test_a_token_urlsafe_32_value_is_accepted() -> None:
    raw = secrets.token_urlsafe(32)

    assert len(raw) == 43  # the shape the grammar is derived from
    OneTimeToken(raw)


def test_every_character_class_of_the_alphabet_is_accepted() -> None:
    OneTimeToken(_VALID)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("", id="empty"),
        pytest.param("a" * 42, id="one-short"),
        pytest.param("a" * 44, id="one-long"),
        pytest.param("a" * 42 + "+", id="plus-is-standard-base64-not-urlsafe"),
        pytest.param("a" * 42 + "/", id="slash-is-standard-base64-not-urlsafe"),
        pytest.param("a" * 42 + "=", id="padding"),
        pytest.param("a" * 42 + " ", id="space"),
        pytest.param("a" * 42 + "\n", id="trailing-newline"),
        pytest.param(" " + "a" * 42, id="leading-space"),
        pytest.param("a" * 42 + "é", id="non-ascii-letter"),
        pytest.param("a" * 42 + "٣", id="non-ascii-digit"),
    ],
)
def test_a_value_that_is_not_43_urlsafe_base64_characters_is_refused(bad: str) -> None:
    with pytest.raises(InvalidOneTimeToken):
        OneTimeToken(bad)


def test_the_refusal_message_never_contains_the_value() -> None:
    near_miss = "SECRETNEARMISS" + "a" * 20  # 34 characters: refused, and most of a token anyway

    with pytest.raises(InvalidOneTimeToken) as raised:
        OneTimeToken(near_miss)

    assert "SECRETNEARMISS" not in str(raised.value)
    assert "SECRETNEARMISS" not in repr(raised.value)
    assert all("SECRETNEARMISS" not in str(arg) for arg in raised.value.args)


def test_repr_is_exactly_the_mask() -> None:
    assert repr(OneTimeToken(_VALID)) == _MASKED


def test_str_is_exactly_the_mask() -> None:
    assert str(OneTimeToken(_VALID)) == _MASKED


def test_format_is_exactly_the_mask() -> None:
    assert f"{OneTimeToken(_VALID)}" == _MASKED


def test_format_with_a_spec_still_hides_the_value() -> None:
    formatted = format(OneTimeToken(_VALID), "")

    assert formatted == _MASKED
    assert _VALID not in f"{OneTimeToken(_VALID)!r} {OneTimeToken(_VALID)!s}"


def test_a_percent_r_log_line_hides_the_value() -> None:
    assert "token=%r" % (OneTimeToken(_VALID),) == f"token={_MASKED}"  # noqa: UP031


def test_reveal_returns_the_plaintext() -> None:
    assert OneTimeToken(_VALID).reveal() == _VALID


def test_a_one_time_token_is_frozen() -> None:
    token = OneTimeToken(_VALID)

    with pytest.raises(dataclasses.FrozenInstanceError):
        token.value = "b" * 43  # type: ignore[misc]


def test_the_error_class_is_a_domain_error_carrying_no_attributes_but_its_message() -> None:
    error = InvalidOneTimeToken()

    assert vars(error) == {}
