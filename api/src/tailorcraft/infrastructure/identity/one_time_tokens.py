"""`OneTimeTokenPort` over `secrets`, and the hashing of a token a browser presents (slice 2.5,
technical plan §0.4, ADR-0020 §7).

Two directions, one hash:

- **Minting** (`SecretsOneTimeTokenMinter.mint`) happens in the worker only. The delivery use case
  stores the hash, commits, and hands the plaintext to the mail adapter, so the plaintext exists in
  one process's memory and in one mail, never in the broker.
- **Presenting** (`hash_presented`) happens in the confirm and reset-confirm routes. The token comes
  back from the link's fragment, is checked against the grammar the minter produces, and is hashed
  here, so `ConfirmRegistration` and `ResetPassword` receive a `TokenHash` and never a plaintext. That
  is the refresh cookie's shape (`api/refresh_cookie.py`), and both use the same SHA-256 helper
  (`token_hashing.py`), so a token hashes the same way at mint and at lookup.

**The grammar check comes first** (V-32): a value that is not 43 URL-safe characters (empty, padded,
a query-string remnant, a JWT pasted by mistake) never becomes a lookup key. The grammar is the
domain's `OneTimeToken`, the type the minter returns, not a second copy of the pattern here.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Final

from tailorcraft.domain.identity.errors import InvalidOneTimeToken
from tailorcraft.domain.identity.value_objects import MintedOneTimeToken, OneTimeToken, TokenHash
from tailorcraft.infrastructure.identity.token_hashing import sha256_token_hash

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import OneTimeTokenPort

# 32 bytes from the OS CSPRNG: unpadded URL-safe base64 of 32 bytes is always exactly 43 characters
# of `[A-Za-z0-9_-]`, the shape `OneTimeToken` refuses anything else of.
_TOKEN_BYTES: Final = 32


class SecretsOneTimeTokenMinter:
    """Mint a one-time token from `secrets.token_urlsafe(32)` and its SHA-256 hash.

    Synchronous, for the port's reason: 32 random bytes and one hash are microseconds with no I/O,
    and a thread hop would cost more than the work. No state, no seam: there is nothing a test needs
    to replace in a random number, and a seeded minter would be a production foot-gun.
    """

    def mint(self) -> MintedOneTimeToken:
        raw = secrets.token_urlsafe(_TOKEN_BYTES)
        return MintedOneTimeToken(token=OneTimeToken(raw), token_hash=sha256_token_hash(raw))


def hash_presented(raw: str) -> TokenHash | None:
    """The hash of a token presented in a link, or `None` when it is not shaped like one we mint.

    `None` is the route's `link_invalid` with `reason=malformed`, decided **before any database
    read** (V-32). The plaintext is not kept: the `OneTimeToken` built here exists only to apply
    the grammar and is dropped at return.
    """
    try:
        OneTimeToken(raw)
    except InvalidOneTimeToken:
        return None
    return sha256_token_hash(raw)


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_one_time_token_port(minter: SecretsOneTimeTokenMinter) -> None:
        _: OneTimeTokenPort = minter
