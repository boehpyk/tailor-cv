"""The `identity_user` table and the imperative mapping for `User` (ADR-0007, technical plan §5).

`properties=` onto the **private** attributes is mandatory for the reason `guest_session.py` gives:
every column name here collides with a read-only `@property` of the same short name on `User`.

**The uniqueness of an email lives here, not in the aggregate.** "No two users share an email" is a
property of the *set* of users, which only the unique index sees atomically (`domain/identity/user.py`
says why the aggregate cannot). `uq_identity_user_email` is therefore not an optimisation or a
belt-and-braces duplicate of a Python check — it is **the** check, and
`SqlAlchemyUserRepository.add` recognises its violation **by this constraint's name**. Renaming the
constraint is a breaking change to that repository.

**No foreign key to `identity_guest_session`** (AC-15). A registered user is not a promoted guest
session (ADR-0010), and a cascade from a guest session would let the 24-hour purge reach a
registered user's row — the one thing FR-6 says it must never touch.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Table
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.identity.user import User
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import (
    EmailAddressType,
    PasswordHashType,
    UserIdType,
)

user_table = Table(
    "identity_user",
    metadata,
    Column("id", UserIdType, primary_key=True),
    # VARCHAR(254) via `EmailAddressType`, always the normalized form. `unique=True` renders as
    # `uq_identity_user_email` under `registry.py`'s naming convention — the login look-up's index and
    # the registration race's referee in one (I-5, I-6, AC-16).
    Column("email", EmailAddressType, nullable=False, unique=True),
    # VARCHAR(512), a PHC string. PII-adjacent: never logged, and a failed INSERT's `DETAIL` line —
    # which would quote it — is withheld by the engine's `handle_error` listener (AC-18).
    Column("password_hash", PasswordHashType, nullable=False),
    # Whole-second, as every instant in this schema: the `Clock` port truncates at the source, so a
    # round trip can never change a value (ADR-0007).
    Column("created_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    Column("password_updated_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    # `EmailAddress` has no un-normalized form, so no code path can write one. This binds the paths
    # that are not code: a hand-written `INSERT`, a backfill, a `psql` session. Without it,
    # "A@x.io" beside "a@x.io" would be two users to the unique index and one person to everybody
    # else. Renders as `ck_identity_user_email_normalized` (the `name=` is the constraint_name slot).
    CheckConstraint("email = lower(btrim(email))", name="email_normalized"),
)

mapper_registry.map_imperatively(
    User,
    user_table,
    properties={
        "_id": user_table.c.id,
        "_email": user_table.c.email,
        "_password_hash": user_table.c.password_hash,
        "_created_at": user_table.c.created_at,
        "_password_updated_at": user_table.c.password_updated_at,
    },
)
