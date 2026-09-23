"""`AccessTokenPort` over HS256 JWTs — **the only module in the codebase that imports `jwt`**.

"We decoded it" and "it is ours, current and meant for this API" are different claims, and the gap
between them is this slice's version of "re-validate structured output on receipt" (the failure
contract's N/A table says so). So `verify` is PyJWT's signature and algorithm check *plus* our own
checks of everything PyJWT either does not check or checks against the wrong clock:

- **The algorithm is pinned** (`algorithms=[ALGORITHM]`). Measured against PyJWT 2.15: `alg: none`,
  `NONE`, `hs256`, `HS384`, `HS512`, `RS256`, `ES256` and an unknown `foo` all raise
  `InvalidAlgorithmError` before any signature is computed, with or without a signature segment.
- **`typ` is `at+jwt`** (RFC 9068) — PyJWT never looks at `typ`, so this module does.
- **The claim set is exactly five keys** (AC-21): an extra claim is refused, not ignored. PyJWT's
  `require` only proves presence.
- **`aud` is exactly our string**: PyJWT accepts a *list* that merely contains it (measured), and
  this API never issues one.
- **`iat` and `exp` are integers**: with PyJWT's own time checks off, it no longer type-checks them
  either (measured: `"iat": "soon"` decodes cleanly).
- **`exp` and `iat` are judged against the instant the caller passes** — see `verify`'s comment.

Every refusal is `AccessTokenInvalid(reason)`; an `except Exception` floor makes that true for
whatever a stranger's bytes provoke in PyJWT's large exception tree. **The token is never logged**
here or anywhere: a bearer token in a log line is a login.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

import jwt
from jwt.exceptions import (
    DecodeError,
    ImmatureSignatureError,
    InvalidAlgorithmError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidSubjectError,
    MissingRequiredClaimError,
)

from tailorcraft.domain.identity.errors import AccessTokenInvalid
from tailorcraft.domain.identity.value_objects import (
    AccessTokenRefusal,
    IssuedAccessToken,
    UserId,
)

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import AccessTokenPort

ALGORITHM: Final = "HS256"
ISSUER: Final = "tailorcraft"
AUDIENCE: Final = "tailorcraft-api"
TOKEN_TYPE: Final = "at+jwt"  # noqa: S105 -- the RFC 9068 media type, not a credential

ISSUED_AT_LEEWAY: Final = timedelta(seconds=30)
"""How far in the future `iat` may be (I-38). Skew is bounded to one box — two uvicorn processes,
one kernel clock — and this covers an NTP step. `exp` has **no** leeway: `exp == now` is expired."""

_CLAIMS: Final = frozenset({"iss", "aud", "sub", "iat", "exp"})


class JwtAccessTokens:
    """Issues and verifies the short-lived bearer token (ADR-0008).

    Constructed per request by the composition root from `settings.jwt_signing_key` and
    `settings.access_token_ttl_minutes` — it holds two values and does no I/O, so there is nothing
    to keep alive.
    """

    def __init__(self, signing_key: str, ttl: timedelta) -> None:
        # Messages name the rule, never the value — this one is a secret.
        if not signing_key:
            raise ValueError("the access-token signing key must not be empty")
        if ttl <= timedelta(0):
            raise ValueError("the access-token lifetime must be positive")
        self._key = signing_key
        self._ttl = ttl

    def issue(self, user_id: UserId, at: datetime) -> IssuedAccessToken:
        """Mint a token for `user_id` issued at `at`. Claims are exactly `{iss, aud, sub, iat, exp}`
        (AC-21) — no email, no login id, no guest session id: a JWT is readable by anyone holding it,
        and every claim added is a fact disclosed to every script that sees the header."""
        issued_at = _epoch_seconds(at)
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(user_id.value),
            "iat": issued_at,
            "exp": issued_at + int(self._ttl.total_seconds()),
        }
        token = jwt.encode(claims, self._key, algorithm=ALGORITHM, headers={"typ": TOKEN_TYPE})
        return IssuedAccessToken(token=token, expires_in=self._ttl)

    def verify(self, token: str, at: datetime) -> UserId:
        """The user `token` speaks for, as of `at`, or `AccessTokenInvalid(reason)`."""
        # Outside the floor on purpose: a naive `at` is a caller's bug, and the floor would disguise
        # it as a stranger's malformed token.
        now = _epoch_seconds(at)
        try:
            return self._verify(token, now)
        except AccessTokenInvalid:
            raise
        except Exception:
            # The floor. PyJWT's exception tree is large and a stranger chooses the input; any
            # failure not named below is "we could not read this", and it must still be a 401 rather
            # than a 500. `from None`: the frame holds the token.
            raise AccessTokenInvalid(AccessTokenRefusal.MALFORMED) from None

    def _verify(self, token: str, now: int) -> UserId:
        try:
            decoded = jwt.decode_complete(
                token,
                self._key,
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=ISSUER,
                # PyJWT's own `exp`/`iat` checks are OFF, and both are checked below against the
                # caller's instant instead. PyJWT reads the wall clock (`datetime.now(tz=UTC)`),
                # and this codebase's time comes from one port — the `Clock` — so a test can move
                # it and every expiry decision agrees with every other timestamp in the request.
                # Leaving PyJWT's check on as well would be a second clock that no test controls.
                options={
                    "require": ["exp", "iat", "sub", "iss", "aud"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except InvalidAlgorithmError:
            raise AccessTokenInvalid(AccessTokenRefusal.BAD_ALGORITHM) from None
        except InvalidSignatureError:
            # Before `DecodeError`, which it subclasses (measured against PyJWT 2.15).
            raise AccessTokenInvalid(AccessTokenRefusal.BAD_SIGNATURE) from None
        except DecodeError:
            raise AccessTokenInvalid(AccessTokenRefusal.MALFORMED) from None
        except (
            MissingRequiredClaimError,
            InvalidIssuerError,
            InvalidAudienceError,
            InvalidSubjectError,
            ImmatureSignatureError,
        ):
            raise AccessTokenInvalid(AccessTokenRefusal.BAD_CLAIMS) from None

        header = decoded["header"]
        payload = decoded["payload"]
        if not isinstance(header, dict) or header.get("typ") != TOKEN_TYPE:
            raise AccessTokenInvalid(AccessTokenRefusal.BAD_CLAIMS)
        if not isinstance(payload, dict) or set(payload) != _CLAIMS:
            raise AccessTokenInvalid(AccessTokenRefusal.BAD_CLAIMS)
        if payload["aud"] != AUDIENCE or payload["iss"] != ISSUER:
            raise AccessTokenInvalid(AccessTokenRefusal.BAD_CLAIMS)
        issued_at, expires_at = payload["iat"], payload["exp"]
        if not (_is_int(issued_at) and _is_int(expires_at)):
            raise AccessTokenInvalid(AccessTokenRefusal.BAD_CLAIMS)
        user_id = _parse_subject(payload["sub"])

        if expires_at <= now:  # no leeway: `exp == now` is expired (AC-21)
            raise AccessTokenInvalid(AccessTokenRefusal.EXPIRED)
        if issued_at > now + int(ISSUED_AT_LEEWAY.total_seconds()):
            raise AccessTokenInvalid(AccessTokenRefusal.ISSUED_IN_FUTURE)
        return user_id


def _epoch_seconds(at: datetime) -> int:
    """Whole epoch seconds of an aware instant. A naive datetime is refused rather than guessed at:
    `.timestamp()` would silently read it as the *server's* local time."""
    if at.tzinfo is None:
        raise ValueError("an instant must be timezone-aware")
    return int(at.astimezone(UTC).timestamp())


def _is_int(value: object) -> bool:
    # `bool` is an `int` subclass, and `"iat": true` is not a time.
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_subject(sub: object) -> UserId:
    """`sub` back into a `UserId`, accepting only the canonical form `issue` writes. `UUID()` also
    accepts braces, a `urn:uuid:` prefix and upper case; a token carrying one of those was not made
    here, so it is refused rather than normalised."""
    if not isinstance(sub, str):
        raise AccessTokenInvalid(AccessTokenRefusal.BAD_CLAIMS)
    try:
        parsed = UUID(sub)
    except ValueError:
        raise AccessTokenInvalid(AccessTokenRefusal.BAD_CLAIMS) from None
    if str(parsed) != sub:
        raise AccessTokenInvalid(AccessTokenRefusal.BAD_CLAIMS)
    return UserId(parsed)


if TYPE_CHECKING:
    # Makes mypy prove the class structurally satisfies the port. Never executed.
    def _assert_implements_access_token_port(tokens: JwtAccessTokens) -> None:
        _: AccessTokenPort = tokens
