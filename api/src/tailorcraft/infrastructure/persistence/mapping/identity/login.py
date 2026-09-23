"""`identity_login` mapped to `Login`, and `identity_retired_refresh_token` as a plain Core `Table`
(ADR-0007, ADR-0020, technical plan §3 and §5).

**`version` is an ordinary column, not the mapper's `version_id_col`** — a deliberate departure from
`tailoring_run.py`, which does declare one. SQLAlchemy's `version_id_col` enforces the check from
inside a flush and raises `StaleDataError` there, and a failed flush expires the identity map on its
way out (CLAUDE.md's 1.4 lesson). The technical plan chose an explicit
`UPDATE … WHERE id = :id AND version = :loaded` issued by `SqlAlchemyLoginRepository.save_rotation`
instead: the check is a statement the repository wrote and can read the row count of, not a side
effect of a flush it did not ask for. **A reader who "fixes" this by adding `version_id_col` would make
the ORM bump `version` a second time on any flush of a dirty `Login`** — the repository's docstring
records how it keeps the ORM from flushing one at all.

**`identity_retired_refresh_token` is deliberately not mapped.** It is a lookup index — "whose token
was this, and at which generation?" — never loaded as a collection on `Login` and never an entity
with behaviour. The repository writes and reads it with Core. A `relationship()` would invite
loading every retired hash of a long-lived login to answer a question one indexed `SELECT` answers.

**Neither table has a foreign key to `identity_guest_session`** (AC-15; `user.py` says why). The only
foreign keys point *up* the identity chain — retired token → login → user — each `ON DELETE CASCADE`,
which is what makes revocation a single `DELETE` of the login (ADR-0020).

**No index on `expires_at`**, on purpose (technical plan §5): the expired-login sweep has no slice
yet, and an index nobody queries is write cost on the hottest row in the schema — a login is updated
every fifteen minutes per open tab.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, Table
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.identity.login import Login
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import (
    LoginIdType,
    TokenHashType,
    UserIdType,
)

login_table = Table(
    "identity_login",
    metadata,
    Column("id", LoginIdType, primary_key=True),
    # Indexed (`ix_identity_login_user_id`) because Postgres does not index a foreign key's
    # referencing side by itself: without it, deleting a user scans this table for the cascade, and
    # the future "log out everywhere" has nothing to seek on.
    Column(
        "user_id",
        UserIdType,
        ForeignKey(user_table.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("created_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    # Absolute (OQ-9): rotation never moves it.
    Column("expires_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    Column("generation", Integer, nullable=False),
    # CHAR(64), the SHA-256 hex of the current refresh token. Unique
    # (`uq_identity_login_current_token_hash`) because it is how a refresh request is resolved back
    # to its login — the refresh path's one index seek.
    Column("current_token_hash", TokenHashType, nullable=False, unique=True),
    Column("rotated_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    # The repository's optimistic-concurrency counter (see the module docstring). No
    # `server_default`: unlike `tailoring_run.version`, this column is born with its table, so there
    # is no previous application version inserting rows that do not know it.
    Column("version", Integer, nullable=False),
    # `Login.start` refuses a non-positive lifetime; this binds what Python cannot reach.
    CheckConstraint("expires_at > created_at", name="expires_after_created"),
    CheckConstraint("generation >= 1", name="generation_positive"),
    # An equality between two predicates rather than an implication, as in `tailoring_run.py`, so it
    # refuses **both** bad pairings: a first-generation login claiming a rotation, and a rotated one
    # with no instant. That second one is what would break `Login.judge_retired`'s grace arithmetic.
    CheckConstraint("(generation = 1) = (rotated_at IS NULL)", name="rotated_iff_rotated"),
)

retired_refresh_token_table = Table(
    "identity_retired_refresh_token",
    metadata,
    # The primary key is the hash, not a surrogate: this table exists to answer "is this hash one I
    # issued, and when?", so the lookup column *is* the identity. Its uniqueness is also the second
    # half of I-25 — `pk_identity_retired_refresh_token` refuses the same retired hash twice, which
    # `save_rotation` recognises by name as `LoginConcurrentlyRotated`.
    Column("token_hash", TokenHashType, primary_key=True),
    # Indexed (`ix_identity_retired_refresh_token_login_id`) for the cascade from `identity_login`:
    # without it every revocation scans the whole retired table.
    Column(
        "login_id",
        LoginIdType,
        ForeignKey(login_table.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("generation", Integer, nullable=False),
    Column("retired_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
)

mapper_registry.map_imperatively(
    Login,
    login_table,
    properties={
        "_id": login_table.c.id,
        "_user_id": login_table.c.user_id,
        "_created_at": login_table.c.created_at,
        "_expires_at": login_table.c.expires_at,
        "_generation": login_table.c.generation,
        "_current_token_hash": login_table.c.current_token_hash,
        "_rotated_at": login_table.c.rotated_at,
        "_version": login_table.c.version,
    },
)
