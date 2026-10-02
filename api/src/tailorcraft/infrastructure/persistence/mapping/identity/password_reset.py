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

from collections.abc import Iterable
from typing import Final, assert_never

from sqlalchemy import CheckConstraint, Column, ForeignKey, Table, event
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.value_objects import (
    AddressedReset,
    EmailAddress,
    IssuedReset,
    ResetTarget,
    UserId,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
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


# --------------------------------------------------------------------------------------------------
# The mapping, and `ResetTarget` ↔ (`email`, `user_id`) — translated here and nowhere else.
# --------------------------------------------------------------------------------------------------
#
# `Owner`'s aggregates (`BaseCv`, `TailoringRun`, …) hold the two halves as two private attributes and
# translate in the domain (`_assign_owner`). `PasswordReset` holds **one**, `_target`, so the
# translation is this module's. The ORM's tool for "one attribute, several columns" is `composite()`,
# and ADR-0007 forbids it (it needs `__composite_values__` on the domain class — a persistence hook
# in `domain/`). Instead the two columns are mapped under names the domain never declares,
# `_target_email` and `_target_user_id`, and two event hooks keep them and `_target` in step:
#
# - **row → aggregate** (`load`, `refresh`): build `_target` from the two columns. `refresh` matters
#   as much as `load`: `lock_by_token_hash`'s `populate_existing` overwrites an instance already in
#   the identity map, which fires `refresh`, not `load`, and a stale `_target` would survive it.
# - **aggregate → row** (`before_insert`, `before_update`): write the two columns from `_target`.
#   `PasswordReset.issue` changes only `_target`, which the mapper does not instrument, so an issued
#   reset is never "dirty" to the ORM and `before_update` would not fire for it; that is why
#   `SqlAlchemyPasswordResetRepository.save_issued` is Core and derives the columns with
#   `target_columns` itself. `before_update` is kept so a flush of a reset that *is* dirty can never
#   write columns that disagree with `_target`.
#
# A row whose columns are both set or both `NULL` is refused by
# `ck_identity_password_reset_exactly_one_target`, so `_target_of` never meets one from the database;
# it raises rather than guessing if it does.


def target_columns(target: ResetTarget) -> tuple[EmailAddress | None, UserId | None]:
    """`(email, user_id)` for `target` — the one aggregate → row translation, shared with the
    repository's Core `UPDATE`."""
    match target:
        case AddressedReset(email=email):
            return email, None
        case IssuedReset(user_id=user_id):
            return None, user_id
        case _:
            assert_never(target)


def _target_of(email: EmailAddress | None, user_id: UserId | None) -> ResetTarget:
    """The row → aggregate translation."""
    if email is not None and user_id is None:
        return AddressedReset(email)
    if user_id is not None and email is None:
        return IssuedReset(user_id)
    # Unreachable through the CHECK; an identifier-free message, since `email` is PII.
    raise ValueError("identity_password_reset row has not exactly one of email and user_id")


mapper_registry.map_imperatively(
    PasswordReset,
    password_reset_table,
    properties={
        "_id": password_reset_table.c.id,
        # The two halves of `_target` (see above): names the domain class never declares.
        "_target_email": password_reset_table.c.email,
        "_target_user_id": password_reset_table.c.user_id,
        "_token_hash": password_reset_table.c.token_hash,
        "_requested_at": password_reset_table.c.requested_at,
        "_expires_at": password_reset_table.c.expires_at,
        "_issued_at": password_reset_table.c.issued_at,
    },
)


_TARGET_COLUMNS: Final = frozenset({"_target_email", "_target_user_id"})


def _target_from_row(reset: PasswordReset) -> None:
    # `getattr`/`setattr` by name: the two mapped attributes exist only on the mapped class, and
    # declaring them on `PasswordReset` would put this module's vocabulary in the domain.
    email: EmailAddress | None = getattr(reset, "_target_email")  # noqa: B009
    user_id: UserId | None = getattr(reset, "_target_user_id")  # noqa: B009
    reset._target = _target_of(email, user_id)


def _on_load(reset: PasswordReset, _context: object) -> None:
    _target_from_row(reset)


def _on_refresh(reset: PasswordReset, _context: object, attrs: Iterable[str] | None) -> None:
    # `attrs` is `None` for a full refresh (`populate_existing`). A partial one that loaded neither
    # half leaves `_target` alone — reading an unloaded column here would be a lazy load, which an
    # `AsyncSession` refuses.
    if attrs is None or not _TARGET_COLUMNS.isdisjoint(attrs):
        _target_from_row(reset)


def _columns_from_target(_mapper: object, _connection: object, reset: PasswordReset) -> None:
    email, user_id = target_columns(reset._target)
    setattr(reset, "_target_email", email)  # noqa: B010
    setattr(reset, "_target_user_id", user_id)  # noqa: B010


event.listen(PasswordReset, "load", _on_load)
event.listen(PasswordReset, "refresh", _on_refresh)
event.listen(PasswordReset, "before_insert", _columns_from_target)
event.listen(PasswordReset, "before_update", _columns_from_target)
