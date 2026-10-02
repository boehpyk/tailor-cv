"""`identity_pending_registration` mapped to `PendingRegistration` (ADR-0007, ADR-0027, technical plan
§3 and §5).

**A sign-up that has not proven its address, never an unverified user** (technical plan §0.1). The
row holds what a `User` will be built from — the normalized address and the password's hash, made at
request time so the confirm path never sees a plaintext — and, once the worker has minted a link, the
SHA-256 of that link's token. It lives at most its TTL: `ConfirmRegistration` deletes it, the
delivery task deletes it when it finds it expired, and the identity token sweep deletes whatever is
left (`ix_identity_pending_registration_expires_at`).

**One row per address** (`uq_identity_pending_registration_email`, §0.3): "newest wins" is a property
of the set, which only the unique index sees atomically, and
`SqlAlchemyPendingRegistrationRepository.put` is one `INSERT … ON CONFLICT (email) DO UPDATE` against
it — no read first, so a second registration for the same address is the same statement as the first.

**No foreign key anywhere**, in either direction. There is no account to point at yet, and nothing
points here: the broker carries the id (§0.4), never a row reference, so a delivery task whose row
was superseded simply finds nothing.

**Two columns carry a credential-shaped value**, `password_hash` (a PHC string) and `token_hash` (a
SHA-256 hex digest). The plaintext token exists in the worker's memory and in the mail, nowhere else.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Table
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import (
    EmailAddressType,
    PasswordHashType,
    PendingRegistrationIdType,
    TokenHashType,
)

pending_registration_table = Table(
    "identity_pending_registration",
    metadata,
    Column("id", PendingRegistrationIdType, primary_key=True),
    # VARCHAR(254) via `EmailAddressType` — `identity_user.email`'s type, so the two compare like with
    # like (technical plan §5 wrote `text`; reusing the existing type is the point of having one).
    # `unique=True` renders `uq_identity_pending_registration_email`: the supersede's conflict target.
    Column("email", EmailAddressType, nullable=False, unique=True),
    # VARCHAR(512) via `PasswordHashType`, `identity_user.password_hash`'s type, for the same reason.
    Column("password_hash", PasswordHashType, nullable=False),
    # Whole-second, as every instant in this schema (ADR-0007).
    Column("requested_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    # Indexed (`ix_identity_pending_registration_expires_at`) for the sweep's
    # `ORDER BY expires_at LIMIT :n` (technical plan §0.9).
    Column("expires_at", TIMESTAMP(timezone=True, precision=0), nullable=False, index=True),
    # CHAR(64), `NULL` until the worker issues the link. Unique
    # (`uq_identity_pending_registration_token_hash`): it is how a presented link is resolved back to
    # its row, and Postgres's UNIQUE admits any number of `NULL`s, so unissued rows never collide.
    Column("token_hash", TokenHashType, nullable=True, unique=True),
    Column("issued_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    # `PendingRegistration`'s invariant, `token_hash is None ⇔ issued_at is None`, bound for the
    # writes that are not the aggregate's — and for `save_issued`'s Core `UPDATE`, which writes both
    # columns by hand. An equality between predicates refuses both bad pairings in one expression.
    CheckConstraint("(token_hash IS NULL) = (issued_at IS NULL)", name="issued_together"),
    # `PendingRegistration.request` refuses a non-positive TTL; this binds what Python cannot reach.
    CheckConstraint("expires_at > requested_at", name="expires_after_request"),
)

mapper_registry.map_imperatively(
    PendingRegistration,
    pending_registration_table,
    properties={
        "_id": pending_registration_table.c.id,
        "_email": pending_registration_table.c.email,
        "_password_hash": pending_registration_table.c.password_hash,
        "_requested_at": pending_registration_table.c.requested_at,
        "_expires_at": pending_registration_table.c.expires_at,
        "_token_hash": pending_registration_table.c.token_hash,
        "_issued_at": pending_registration_table.c.issued_at,
    },
)
