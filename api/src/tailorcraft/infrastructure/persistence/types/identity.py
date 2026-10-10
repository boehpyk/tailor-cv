"""`TypeDecorator`s for the `identity` context's value objects (ADR-0007).

One class per value object, as in `types/intake.py`. Slice 2.1 adds five beside `GuestSessionIdType`;
slice 2.5 adds the two one-time-token aggregates' ids (`PendingRegistrationIdType`,
`PasswordResetIdType`) and reuses the other four as they are; slice 4.1 adds `RoleType`.
Three of them carry a **credential-shaped** value (`PasswordHash`, `TokenHash`) or PII
(`EmailAddress`); none of them logs, and none of them has anything to log — a `TypeDecorator` is a
pure translation, and a failure inside it surfaces through the engine's `handle_error` listener with
the bound values already withheld (`persistence/database.py`, AC-18).

**Every `process_result_value` rebuilds the value object through its own constructor**, so a row that
bypassed the domain (a hand-written `UPDATE`, a bad backfill) fails loudly on the way back in rather
than handing the application a value its type promises cannot exist. `EmailAddress.__post_init__`
refuses a non-normalized value, which is the Python half of `ck_identity_user_email_normalized`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import CHAR, String
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    LoginId,
    PasswordHash,
    PasswordResetId,
    PendingRegistrationId,
    Role,
    TokenHash,
    UserId,
)


class GuestSessionIdType(TypeDecorator[GuestSessionId]):
    """`identity_guest_session.id` and `intake_base_cv.guest_session_id` — a native Postgres `UUID`
    carrying a `GuestSessionId` rather than a bare `UUID`, so a query result rehydrates the typed id
    the domain expects instead of handing application code a primitive to re-wrap by hand."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: GuestSessionId | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> GuestSessionId | None:
        if value is None:
            return None
        return GuestSessionId(value)


class UserIdType(TypeDecorator[UserId]):
    """`identity_user.id` and `identity_login.user_id` — a native `UUID` carrying a `UserId`, so a
    `LoginId` cannot be bound where a `UserId` is meant without mypy noticing."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: UserId | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> UserId | None:
        if value is None:
            return None
        return UserId(value)


class LoginIdType(TypeDecorator[LoginId]):
    """`identity_login.id` and `identity_retired_refresh_token.login_id` — a native `UUID`
    carrying a `LoginId`."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: LoginId | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> LoginId | None:
        if value is None:
            return None
        return LoginId(value)


class EmailAddressType(TypeDecorator[EmailAddress]):
    """`identity_user.email` — `VARCHAR(254)`, the RFC 5321 path limit `EmailAddress` enforces.

    Binds the **already-normalized** value: `EmailAddress` has no un-normalized form, so the unique
    index `uq_identity_user_email` compares like with like and "A@x.io" cannot sit beside "a@x.io".
    Rebuilt with the constructor, not `parse`, on the way out — `parse` would silently normalize a
    row that was written wrong, where the constructor refuses it.
    """

    impl = String(254)
    cache_ok = True

    def process_bind_param(self, value: EmailAddress | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> EmailAddress | None:
        if value is None:
            return None
        return EmailAddress(value)


class PasswordHashType(TypeDecorator[PasswordHash]):
    """`identity_user.password_hash` — `VARCHAR(512)`, a PHC string (`$argon2id$…`). Variable width
    because the string's length depends on the algorithm and its parameters, which is the point of
    the PHC format: a rehash under new parameters changes the length and nothing else."""

    impl = String(512)
    cache_ok = True

    def process_bind_param(self, value: PasswordHash | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> PasswordHash | None:
        if value is None:
            return None
        return PasswordHash(value)


class TokenHashType(TypeDecorator[TokenHash]):
    """`identity_login.current_token_hash` and `identity_retired_refresh_token.token_hash` —
    `CHAR(64)`, a SHA-256 hex digest of a refresh token, fixed-width for the reason
    `identity_guest_session.token_hash` is. Only the hash is ever stored; the plaintext token exists
    in the cookie and in the route that minted it, nowhere else (ADR-0010's pattern)."""

    impl = CHAR(64)
    cache_ok = True

    def process_bind_param(self, value: TokenHash | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> TokenHash | None:
        if value is None:
            return None
        return TokenHash(value)


class PendingRegistrationIdType(TypeDecorator[PendingRegistrationId]):
    """`identity_pending_registration.id` — a native `UUID` carrying a `PendingRegistrationId`, so a
    sign-up that is not yet an account cannot be bound where a `UserId` is meant (ADR-0027)."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(
        self, value: PendingRegistrationId | None, dialect: Dialect
    ) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(
        self, value: Any | None, dialect: Dialect
    ) -> PendingRegistrationId | None:
        if value is None:
            return None
        return PendingRegistrationId(value)


class PasswordResetIdType(TypeDecorator[PasswordResetId]):
    """`identity_password_reset.id` — a native `UUID` carrying a `PasswordResetId` (ADR-0028)."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: PasswordResetId | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> PasswordResetId | None:
        if value is None:
            return None
        return PasswordResetId(value)


class RoleType(TypeDecorator[Role]):
    """`identity_user.role` — `VARCHAR(16)`, `NOT NULL`, one of `ck_identity_user_role_known`'s
    values. Slice 4.1, ADR-0032.

    **An unknown stored value is refused with a `ValueError` naming the column, never the value**
    (AC-10(d), `LayoutTemplateType`'s pattern): the enum's own message would echo the stored string.
    The CHECK makes such a row unreachable through SQL; this is the Python half of the same rule.
    """

    impl = String(16)
    cache_ok = True

    def process_bind_param(self, value: Role | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> Role | None:
        if value is None:
            return None
        try:
            return Role(value)
        except ValueError:
            raise ValueError("identity_user.role holds an unknown role") from None
