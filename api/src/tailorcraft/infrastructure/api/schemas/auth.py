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


class UserResponse(BaseModel):
    """`{"id", "email", "created_at"}` — `GET /api/auth/me`, and `user` inside every
    `AuthenticatedResponse`. Nothing else about an account crosses the wire: no hash, no login id,
    no timestamps of password changes."""

    id: UUID
    email: str
    created_at: datetime


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
