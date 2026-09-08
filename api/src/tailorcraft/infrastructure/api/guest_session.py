"""The guest session cookie: minting, hashing and reading `tc_guest` (ADR-0010).

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

from fastapi import Request, Response

from tailorcraft.infrastructure.settings import Settings

COOKIE_NAME = "tc_guest"

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
    """Read the raw `tc_guest` cookie off a request, or `None` if it is absent.

    Returns the raw token, not its hash — hashing is the caller's job (typically immediately, via
    `hash_guest_token`, to look the session up) so that this function has exactly one
    responsibility: reading the cookie.
    """
    return request.cookies.get(COOKIE_NAME)


def set_guest_cookie(response: Response, token: str, settings: Settings) -> None:
    """Set the `tc_guest` cookie on a response for a freshly minted (or renewed) session.

    Attributes, per AC-10:
    - `HttpOnly` — never readable from JavaScript; this is a bearer credential, not UI state.
    - `SameSite=Lax` — sent on top-level navigation and same-site requests, not on a cross-site
      POST, which is the right default for a cookie that authorizes a mutation.
    - `Path=/` — the whole app is one guest session, not one path.
    - `Max-Age` — derived from `settings.guest_retention_hours`, never hard-coded, so the cookie's
      lifetime cannot silently drift from the retention promise it represents.
    - `Secure` whenever `settings.is_production` — plain HTTP over `localhost` in dev must still be
      able to set and read the cookie, so `Secure` is conditional rather than always on.
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.guest_retention_hours * _SECONDS_PER_HOUR,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
    )
