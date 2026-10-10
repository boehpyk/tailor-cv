"""Request and response schemas for `/api/auth` (technical plan §4) — the wire format, and nothing else.

**The bounds here are the boundary's, not the domain's.** `email ≤ 320` and `password ≤ 1024` refuse
the absurd before anything parses it, as `schemas/posting.py`'s 40,000-against-30,000 does: an
address of 255…320 characters still reaches `EmailAddress.parse` and is answered `invalid_email`
with the domain's reason, and a registration password of 13…1024 code points still reaches
`PasswordPolicy` and is answered `password_too_long` naming 128. What the schema refuses is a
generic `validation_error` (I-8).

**`password` is a `SecretStr`** from the moment it is parsed, so not even FastAPI's own validation
holds it as a printable `str`: its `repr` is asterisks, and the 422 handler never echoes `input`
(`main.py`). `min_length=1` refuses an empty password as a malformed request on both endpoints —
there is no empty password to judge — which also keeps `password_too_short` a registration-only code,
as the contract has it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from tailorcraft.domain.identity.value_objects import Role

EMAIL_MAX_LENGTH = 320
PASSWORD_MAX_LENGTH = 1024


class CredentialsRequest(BaseModel):
    """`{"email": str, "password": str}` — the body of `register` and of `login`, one shape.

    `email` is a bounded `str` and not Pydantic's `EmailStr`, for `FetchedJobPostingRequest.url`'s
    reason: `EmailAddress` is the type that decides what an address is, and a second, different rule
    set at the boundary would disagree with it the first time either changed.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=EMAIL_MAX_LENGTH)
    password: SecretStr = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class DeleteAccountRequest(BaseModel):
    """`POST /api/auth/delete-account` — `{"password": str}` (slice 2.2, technical plan §4).

    The password is re-asked even though the bearer is valid: deleting an account is the one action
    a borrowed, unlocked laptop must not be able to take. A `SecretStr` from the moment it is parsed,
    for `CredentialsRequest`'s reason, with the same bounds. **No email field**: the account is the
    bearer's, and the per-email limiter's key comes from the user row, never from the body.
    """

    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


TOKEN_MAX_LENGTH = 128
"""The boundary's bound on a presented link token. A token we mint is exactly 43 characters; the
schema refuses only the absurd (a pasted page, a megabyte of junk) as `validation_error`, and
everything up to 128 reaches the route's grammar check (`one_time_tokens.hash_presented`), which
answers 400 `link_invalid` for anything not shaped like a token we mint — before any database read.
So an empty, padded or truncated token is `link_invalid`, never a 422: no `min_length` here."""


class TokenRequest(BaseModel):
    """`POST /api/auth/registration/confirm` — `{"token": str}` (slice 2.5, technical plan §4).

    The token travels in the **body**, never in the URL: the client reads it from the link's
    fragment, which no server, proxy or `Referer` ever sees, and posts it here."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(max_length=TOKEN_MAX_LENGTH)


class EmailRequest(BaseModel):
    """`POST /api/auth/password-reset` — `{"email": str}` (slice 2.5, technical plan §4).

    A bounded `str`, not `EmailStr`, for `CredentialsRequest`'s reason: `EmailAddress.parse` is the
    one rule set that decides what an address is."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=EMAIL_MAX_LENGTH)


class ResetConfirmRequest(BaseModel):
    """`POST /api/auth/password-reset/confirm` — `{"token": str, "password": str}` (slice 2.5).

    `token` as in `TokenRequest`; `password` a `SecretStr` with `CredentialsRequest`'s bounds, so the
    policy (12…128 code points, not the address) answers `password_*` from the domain, and only the
    absurd is a schema `validation_error`."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(max_length=TOKEN_MAX_LENGTH)
    password: SecretStr = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class UserResponse(BaseModel):
    """`{"id", "email", "created_at", "role"}` — `GET /api/auth/me`, and `user` inside every
    `AuthenticatedResponse`. Nothing else about an account crosses the wire: no hash, no login id,
    no timestamps of password changes.

    `role` (slice 4.1) serializes as its string value (`"user"` or `"admin"`). It tells the client
    whether to *show* the admin link, nothing more: the server re-reads the role on every
    `/api/admin` request, and it is never a token claim (technical plan §0.3)."""

    id: UUID
    email: str
    created_at: datetime
    role: Role


class AuthenticatedResponse(BaseModel):
    """What `register`, `login` and `refresh` return (RFC 6749 §5.1's shape, AC-33).

    `expires_in` is **relative whole seconds**, never an absolute instant: the client's own clock may
    be anywhere, and "this lives 900 s" keeps it out of the arithmetic (I-38, AC-38). The refresh
    token is **not** here — it travels only in the `HttpOnly` cookie, where no script can read it.
    """

    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 -- RFC 6750 scheme name, not a secret
    expires_in: int
    user: UserResponse
