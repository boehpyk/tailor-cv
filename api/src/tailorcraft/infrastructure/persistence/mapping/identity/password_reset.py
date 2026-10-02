"""`identity_password_reset` mapped to `PasswordReset` (ADR-0007, ADR-0028, technical plan §3 and
§5).

**Two states, two columns, one fact.** A reset is requested for an *address* — the request path never
looks the account up (§0.2) — and issued to an *account* once the worker has found one, at which point
the address is dropped (data minimisation: the account row holds it). The domain says so with a sum
type, `ResetTarget = AddressedReset | IssuedReset`; a foreign key has exactly one target table and an
address has none, so the table says it with two nullable columns, `email` and `user_id`, and
`ck_identity_password_reset_exactly_one_target` restores "exactly one". `Owner`'s shape (ADR-0022).

**No foreign key to `identity_guest_session`**, and one to `identity_user`, `ON DELETE CASCADE`: an
erased account takes its issued resets with it. Its *addressed* resets have no `user_id` to cascade
from, which is why erasure also deletes by address (`SqlAlchemyAccountData.delete_account`) and why
`email` is indexed.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.registry import metadata
from tailorcraft.infrastructure.persistence.types.identity import (
    EmailAddressType,
    PasswordResetIdType,
    TokenHashType,
    UserIdType,
)

password_reset_table = Table(
    "identity_password_reset",
    metadata,
    Column("id", PasswordResetIdType, primary_key=True),
    # The addressed half of the target: set while the reset is only requested, `NULL` once issued.
    # Indexed (`ix_identity_password_reset_email`) for erasure by address — an addressed reset has no
    # `user_id` for the cascade to find. Not unique: the request path does not supersede (§0.2).
    Column("email", EmailAddressType, nullable=True, index=True),
    # The issued half. `ON DELETE CASCADE` so an erased account takes its issued resets. Indexed
    # (`ix_identity_password_reset_user_id`): the cascade, the supersede in `save_issued` and
    # `remove_all_for_user` all seek on it, and Postgres does not index a referencing column itself.
    Column(
        "user_id",
        UserIdType,
        ForeignKey(user_table.c.id, ondelete="CASCADE"),
        nullable=True,
        index=True,
    ),
    # CHAR(64), `NULL` until issued. Unique (`uq_identity_password_reset_token_hash`): how a presented
    # link is resolved back to its row; any number of unissued `NULL`s coexist.
    Column("token_hash", TokenHashType, nullable=True, unique=True),
    Column("requested_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    # Indexed (`ix_identity_password_reset_expires_at`) for the sweep (technical plan §0.9).
    Column("expires_at", TIMESTAMP(timezone=True, precision=0), nullable=False, index=True),
    Column("issued_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    # The sum type's "exactly one": an inequality between predicates refuses both other states —
    # an address *and* an account, and neither — in one expression.
    CheckConstraint("(email IS NULL) <> (user_id IS NULL)", name="exactly_one_target"),
    # "issued ⇔ the target is an account ⇔ a token hash is present", and `issued_at` with them —
    # `PasswordReset`'s invariant, bound for `save_issued`'s hand-written `UPDATE` above all.
    CheckConstraint(
        "(user_id IS NULL) = (token_hash IS NULL) AND (token_hash IS NULL) = (issued_at IS NULL)",
        name="issued_with_account",
    ),
)
