"""The refresh cookie: minting, hashing, reading, setting and clearing `tc_refresh` (ADR-0020).

**Deliberately parallel to `guest_session.py`** — same shape, same "pure cookie mechanics" scope: this
module knows `secrets`, `hashlib` and FastAPI's `Request`/`Response`, and nothing about `Login` the
aggregate or `LoginRepository` the port. The route mints `(token, hash)`, hands the application only
the `TokenHash`, and puts the plaintext in the cookie; no plaintext refresh token ever reaches
`application/` (AC-10), and `mypy` enforces it because every use case takes a `TokenHash`.

Where it deliberately **differs** from the guest cookie, each difference is a decision:

- `SameSite=Strict`, not `Lax` — this cookie mints bearer credentials, and it is never needed on a
  top-level navigation from another site (ADR-0021 §4).
- `Path=/api/auth`, not `/` — it is sent to the five auth endpoints and to nothing else, so no other
  route can even see it by mistake.
- `Max-Age` is the login's **remaining** absolute lifetime, passed in by the caller — a rotation on
  day 29 must not reset it to 30 days (AC-24, OQ-9).
- **Reading validates the format** (I-40): see `read_refresh_token`.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, TypedDict

from fastapi import Request, Response

from tailorcraft.domain.identity.value_objects import TokenHash
from tailorcraft.infrastructure.settings import Settings

COOKIE_NAME: Final = "tc_refresh"
COOKIE_PATH: Final = "/api/auth"

_TOKEN_BYTES: Final = 32
# `secrets.token_urlsafe(32)` is 32 bytes as unpadded URL-safe base64: always exactly 43 characters
# of `[A-Za-z0-9_-]`. Anything else did not come from `mint_refresh_token`.
_TOKEN_SHAPE: Final = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True, slots=True)
class MintedRefreshToken:
    """The two faces of one freshly minted refresh token: the plaintext for the cookie and the hash
    for `identity_login`. The last place both are held together, for the length of one request. The
    plaintext is kept out of `repr` — this object is exactly what a debugging `print` reaches for."""

    token: str = field(repr=False)
    token_hash: TokenHash


@dataclass(frozen=True, slots=True)
class PresentedRefreshToken:
    """What arrived in `tc_refresh`, when *something* arrived.

    `token_hash` is `None` when the value was not shaped like a token we mint (I-40): a JWT pasted
    into the cookie, a truncated value, anything. The caller treats that exactly like an unknown
    token — 401 `not_signed_in` **and clear the cookie** (the browser holds junk it should drop) —
    without ever using it as a lookup key. That is the difference from "no cookie at all"
    (`read_refresh_token` returns `None`), which is every guest's first page load and clears nothing
    (I-19).
    """

    token_hash: TokenHash | None


def mint_refresh_token() -> MintedRefreshToken:
    """Mint a new opaque refresh token (AC-23): 256 bits from the OS CSPRNG, derived from nothing."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    return MintedRefreshToken(token=token, token_hash=hash_refresh_token(token))


def hash_refresh_token(token: str) -> TokenHash:
    """Hash a plaintext refresh token for storage or lookup.

    **Unsalted SHA-256, not argon2 — and that is correct, not an oversight** (ADR-0010 §3, restated by
    ADR-0020 §7 for this token). A KDF exists to make *guessing* expensive for a low-entropy secret a
    person chose. This token is 256 bits from a CSPRNG: there is no dictionary to run against a stolen
    hash, so a slow hash buys nothing, while it would cost 64 MiB and ~50 ms of argon2 on every
    refresh — every 15 minutes, per tab. A salt is the same argument one level down: it defeats
    precomputation across many low-entropy secrets, and there is no rainbow table for a random
    256-bit value. And a KDF with a random salt could not be *looked up* by at all.
    """
    return TokenHash(hashlib.sha256(token.encode("utf-8")).hexdigest())


def read_refresh_token(request: Request) -> PresentedRefreshToken | None:
    """Read `tc_refresh` off a request: `None` if there is no such cookie, otherwise a
    `PresentedRefreshToken` carrying the hash of a well-formed value, or `None` in its place.

    **The format check is I-40's control.** An access token placed in this cookie is a JWT — three
    dot-separated segments, far longer than 43 characters — and must never become a key into
    `identity_login`. Checking the shape here means no caller can forget to, and the plaintext never
    leaves this module on the read side: what goes back is a hash or nothing.
    """
    raw = request.cookies.get(COOKIE_NAME)
    if raw is None:
        return None
    if _TOKEN_SHAPE.fullmatch(raw) is None:
        return PresentedRefreshToken(token_hash=None)
    return PresentedRefreshToken(token_hash=hash_refresh_token(raw))


def remaining_lifetime_seconds(expires_at: datetime, now: datetime) -> int:
    """Whole seconds from `now` to the login's absolute `expires_at`, never negative — the
    `Max-Age` a rotated cookie carries, so a rotation never extends the login (AC-24)."""
    return max(0, int((expires_at - now).total_seconds()))


def set_refresh_cookie(response: Response, token: str, max_age: int, settings: Settings) -> None:
    """Set `tc_refresh` for a freshly minted or rotated token. `max_age` is the login's remaining
    lifetime (`remaining_lifetime_seconds`), never a constant."""
    response.set_cookie(key=COOKIE_NAME, value=token, max_age=max_age, **_attributes(settings))


def clear_refresh_cookie(response: Response, settings: Settings) -> None:
    """Tell the browser to drop `tc_refresh`: an empty value with `Max-Age=0`, under **exactly** the
    attributes `set_refresh_cookie` uses. A clear with a different `Path` is a different cookie to a
    browser, which ignores it silently and keeps the live one (AC-24)."""
    response.set_cookie(key=COOKIE_NAME, value="", max_age=0, **_attributes(settings))


class _CookieAttributes(TypedDict):
    path: str
    httponly: bool
    samesite: Literal["strict"]
    secure: bool


def _attributes(settings: Settings) -> _CookieAttributes:
    """**The one attribute builder** for set and clear, so the two cannot disagree (AC-24).

    - `HttpOnly` — no script can read it; the whole design keeps credentials off the page.
    - `SameSite=Strict` — never sent on a cross-site request, including a top-level navigation.
    - `Path=/api/auth` — sent to the auth endpoints and nothing else.
    - `Secure` whenever `APP_ENV=production`; plain HTTP on `localhost` in dev must still work.
    - **No `Domain`**, deliberately: a host-only cookie, so sibling `*.samolit.com` apps on the same
      box never receive it. Passing any `domain=` here would widen it to every subdomain.
    """
    return _CookieAttributes(
        path=COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=settings.is_production,
    )
