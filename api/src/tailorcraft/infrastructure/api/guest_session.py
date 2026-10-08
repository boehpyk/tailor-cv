"""The guest session cookie: minting, hashing, reading, setting and clearing `__Host-tc_guest`
(ADR-0010, amendment (c)).

**The name carries a browser-enforced guarantee.** A browser accepts a cookie whose name starts
`__Host-` only if it is `Secure`, has `Path=/` and has **no `Domain`**. That makes it host-only by
rule rather than by our discipline: a sibling `*.samolit.com` app on the same box cannot plant a
guest token here (session fixation), because any cookie it could set for `cv.samolit.com` would have
to carry `Domain=samolit.com`, which the prefix forbids.

- **`Secure` is unconditional now**, in every `APP_ENV`. It used to follow `settings.is_production`
  so plain-HTTP `localhost` could keep the cookie; the prefix requires it, and `localhost` is a
  secure context anyway — Chromium 145 accepts the `Secure` `__Host-` form over plain HTTP on
  `localhost:8080`, `127.0.0.1:8080` and `localhost:5173`, and refuses it without `Secure` (checked
  in the slice's T2).
- **Never pass `domain=`.** Any `Domain` attribute, even this host's own name, makes a browser
  refuse the whole cookie: the guest would silently get a new session on every request.
- **A hard cut, not a migration.** The legacy `tc_guest` is never read (ADR-0010 (c)). Reading it,
  even to migrate it, would promote a planted token into the prefixed cookie. Clearing a planted
  cookie is no control either: the `Cookie` header carries no attributes, so the server cannot see
  which `Domain`/`Path` it was set with, and a sibling host can re-plant it at will. A guest holding
  only `tc_guest` starts a new session.

This module is pure cookie mechanics — it knows about `secrets`, `hashlib`, and FastAPI's
`Request`/`Response`, and nothing about `GuestSession` the aggregate or `GuestSessionRepository` the
port. The composition root (`infrastructure/api/deps.py`) is what turns "a raw token arrived on this
request" into "here is the `GuestSession` it names, or here is a freshly minted one" — that is a use
case's job (`StartGuestSession`), not this module's.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Final, Literal, TypedDict

from fastapi import Request, Response

from tailorcraft.infrastructure.settings import Settings

COOKIE_NAME: Final = "__Host-tc_guest"

# AC-10's literal is 86400 seconds. It is not hard-coded below on purpose: `Max-Age` is derived from
# `settings.guest_retention_hours` so the cookie's lifetime and the guest-data retention promise
# (FR-6, ADR-0006) cannot drift apart — if `Settings.guest_retention_hours`'s default (24) and AC-10's
# literal (86400) ever disagree, the setting wins and 86400 is only its default's arithmetic result.
_SECONDS_PER_HOUR = 3600


@dataclass(frozen=True, slots=True)
class MintedGuestToken:
    """The two faces of one freshly minted token: the raw value that goes into the cookie, and the
    hash that goes into `identity_guest_session.token_hash`. The raw value is never written down on
    our side — this dataclass is the last place both are held together, and only for the duration of
    one request."""

    token: str
    token_hash: str


def mint_guest_token() -> MintedGuestToken:
    """Mint a new opaque guest token (ADR-0010).

    `secrets.token_urlsafe(32)` draws 256 bits from the OS CSPRNG — not derived from the session id,
    the clock, or anything else, which is exactly what rules out a UUIDv7 (whose leading 48 bits are
    a timestamp) as a credential.
    """
    token = secrets.token_urlsafe(32)
    return MintedGuestToken(token=token, token_hash=hash_guest_token(token))


def hash_guest_token(token: str) -> str:
    """Hash a raw token for storage or lookup.

    **Unsalted SHA-256, not argon2/bcrypt, and that is correct, not an oversight.** A KDF like
    argon2 exists to make *guessing* expensive for a **low-entropy** secret — a password, which
    people reuse and choose badly, so an attacker with a stolen hash can try a dictionary against it.
    This token has 256 bits of entropy from a CSPRNG: there is no dictionary to attack, and a slow
    hash would buy nothing while costing a KDF on every single request (ADR-0010 §3). A salt is the
    same story one level down — it defends against precomputation across many *low-entropy* secrets,
    and there is no rainbow table for a 256-bit random value either. "Why isn't this argon2" is
    exactly the question a careful reviewer should ask; the answer is here rather than rediscovered.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_guest_token(request: Request) -> str | None:
    """Read the raw `__Host-tc_guest` cookie off a request, or `None` if it is absent.

    Returns the raw token, not its hash — hashing is the caller's job (typically immediately, via
    `hash_guest_token`, to look the session up) so that this function has exactly one
    responsibility: reading the cookie.
    """
    return request.cookies.get(COOKIE_NAME)


class _CookieAttributes(TypedDict):
    path: str
    httponly: bool
    samesite: Literal["lax"]
    secure: bool


# **The one attribute set** for set and clear (OQ-3, like `refresh_cookie._attributes`): a browser
# keeps a cookie whose deletion names different attributes, so the two must not be able to disagree.
# A constant rather than a builder because nothing in it depends on settings any more.
# - `HttpOnly` — never readable from JavaScript; this is a bearer credential, not UI state.
# - `SameSite=Lax` — sent on top-level navigation and same-site requests, not on a cross-site POST.
# - `Path=/` and `Secure` — both required by the `__Host-` prefix (see the module docstring).
# - **No `domain`** — the prefix forbids it; adding one makes every browser drop the cookie.
_ATTRIBUTES: Final = _CookieAttributes(path="/", httponly=True, samesite="lax", secure=True)


def set_guest_cookie(response: Response, token: str, settings: Settings) -> None:
    """Set `__Host-tc_guest` for a freshly minted (or renewed) session, under `_ATTRIBUTES`.

    `Max-Age` is derived from `settings.guest_retention_hours`, never hard-coded, so the cookie's
    lifetime cannot silently drift from the retention promise it represents (AC-10 of slice 1.1).
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.guest_retention_hours * _SECONDS_PER_HOUR,
        **_ATTRIBUTES,
    )


def clear_guest_cookie(response: Response) -> None:
    """Clear `__Host-tc_guest` — after a claim consumed its session (slice 2.4, ADR-0010: invalidate
    the guest token at the same moment its work leaves it).

    Under exactly `_ATTRIBUTES`, `Secure` included: a clear without it is itself refused by the
    browser under the prefix rule. Starlette's `delete_cookie` sends an empty value with `Max-Age=0`
    (and a past `Expires`).
    """
    response.delete_cookie(key=COOKIE_NAME, **_ATTRIBUTES)
