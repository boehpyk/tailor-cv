"""The one-time token minter and `hash_presented` (slice 2.5, T23, test-after; AC-1's adapter half).

Source of truth: the token grammar in plan §0.4 (`secrets.token_urlsafe(32)`, 43 URL-safe characters),
the hash being SHA-256 hex, and V-32 (a value outside the grammar never becomes a lookup key).
"""

from __future__ import annotations

import hashlib
import re

import pytest

from tailorcraft.infrastructure.identity.one_time_tokens import (
    SecretsOneTimeTokenMinter,
    hash_presented,
)

_GRAMMAR = re.compile(r"[A-Za-z0-9_-]{43}")


def test_a_minted_token_has_the_grammar_of_token_urlsafe_32() -> None:
    minted = SecretsOneTimeTokenMinter().mint()

    assert _GRAMMAR.fullmatch(minted.token.reveal())


def test_a_minted_hash_is_the_sha256_of_the_token() -> None:
    minted = SecretsOneTimeTokenMinter().mint()

    expected = hashlib.sha256(minted.token.reveal().encode("utf-8")).hexdigest()
    assert minted.token_hash.value == expected


def test_a_minted_token_hashes_to_the_same_value_when_it_comes_back_from_a_link() -> None:
    minted = SecretsOneTimeTokenMinter().mint()

    assert hash_presented(minted.token.reveal()) == minted.token_hash


def test_every_mint_is_different() -> None:
    minter = SecretsOneTimeTokenMinter()

    tokens = {minter.mint().token.reveal() for _ in range(50)}

    assert len(tokens) == 50


def test_a_minted_token_never_prints() -> None:
    minted = SecretsOneTimeTokenMinter().mint()
    raw = minted.token.reveal()

    assert raw not in repr(minted.token)
    assert raw not in f"{minted.token}"
    assert raw not in repr(minted)


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "short",
        "A" * 42,
        "A" * 44,
        "A" * 42 + "=",
        "A" * 42 + "+",
        "A" * 42 + "/",
        "A" * 42 + "\n",
        " " + "A" * 43,
        "A" * 42 + "é",
        "token=" + "A" * 37,
        "A" * 43 + "&x=1",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    ],
    ids=[
        "empty",
        "short",
        "42-chars",
        "44-chars",
        "padded",
        "plus",
        "slash",
        "trailing-newline",
        "leading-space",
        "non-ascii",
        "query-remnant",
        "query-suffix",
        "jwt",
    ],
)
def test_a_value_outside_the_grammar_is_not_hashed(presented: str) -> None:
    assert hash_presented(presented) is None


def test_a_well_formed_token_that_was_never_minted_still_hashes() -> None:
    """The grammar says nothing about whether we minted it; that is the database's answer."""
    token = "A" * 43

    result = hash_presented(token)

    assert result is not None
    assert result.value == hashlib.sha256(token.encode()).hexdigest()
