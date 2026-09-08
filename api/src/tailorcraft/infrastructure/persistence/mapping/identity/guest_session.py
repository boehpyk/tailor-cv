"""The `identity_guest_session` table and the imperative mapping for `GuestSession` (ADR-0007).

Column names are written out explicitly rather than derived, per ADR-0007 — a schema readable
without knowing the mapper's rules is worth the extra typing. The `properties=` mapping below is
mandatory, not decorative: every column name here (`id`, `token_hash`, …) collides with a read-only
`@property` of the same short name on `GuestSession`, so mapping by matching column-to-attribute
name (SQLAlchemy's default with no `properties=`) would try to write through those properties and
fail. Naming the *private* attributes (`_id`, `_token_hash`, …) explicitly sidesteps that entirely.
"""

from __future__ import annotations

from sqlalchemy import CHAR, Column, Table
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import GuestSessionIdType

guest_session_table = Table(
    "identity_guest_session",
    metadata,
    Column("id", GuestSessionIdType, primary_key=True),
    # SHA-256 hex digest of the cookie token — always exactly 64 hex characters (ADR-0010), so a
    # fixed-width CHAR is the honest type rather than a VARCHAR that could hold anything shorter.
    # Unique because a token hash is how a request is resolved back to its session; the naming
    # convention in registry.py turns this into `uq_identity_guest_session_token_hash`.
    Column("token_hash", CHAR(64), nullable=False, unique=True),
    Column("created_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    # Indexed **before** the retention job (slice 1.6) that will query
    # `WHERE expires_at < now()` needs it — and before `/health/ready`'s backlog count needs it too.
    # Built now so the first purge run is not also the first sequential scan of a growing table. The
    # naming convention turns this into `ix_identity_guest_session_expires_at`.
    Column("expires_at", TIMESTAMP(timezone=True, precision=0), nullable=False, index=True),
)

mapper_registry.map_imperatively(
    GuestSession,
    guest_session_table,
    properties={
        "_id": guest_session_table.c.id,
        "_token_hash": guest_session_table.c.token_hash,
        "_created_at": guest_session_table.c.created_at,
        "_expires_at": guest_session_table.c.expires_at,
    },
)
