"""`SqlAlchemyPasswordResetRepository` — the `PasswordResetRepository` port (ADR-0028, technical plan
§0.4, §0.7, §0.8, §3).

`SqlAlchemyPendingRegistrationRepository`'s shape: reads through the ORM, every write that carries a
rule in Core, every Core write that can strand an instance expunging it by identity. The one
difference is `ResetTarget`: the aggregate holds one attribute and the row two columns, translated in
`mapping/identity/password_reset.py` alone — `save_issued` asks that module for the columns
(`target_columns`) rather than knowing the shape itself.

**Nothing here logs**, and nothing commits; `CommittingPasswordResetRepository`
(`infrastructure/identity/token_access.py`) makes the request and delivery paths' writes durable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.util import identity_key

from tailorcraft.domain.identity.errors import PasswordResetAlreadyIssued
from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.value_objects import PasswordResetId, TokenHash, UserId
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.mapping.identity.password_reset import (
    password_reset_table,
    target_columns,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tailorcraft.domain.identity.ports import PasswordResetRepository

_RESET_ID: InstrumentedAttribute[PasswordResetId] = cast(
    "InstrumentedAttribute[PasswordResetId]", PasswordReset._id
)
_RESET_TOKEN_HASH: InstrumentedAttribute[TokenHash | None] = cast(
    "InstrumentedAttribute[TokenHash | None]", PasswordReset._token_hash
)

_table = password_reset_table


class SqlAlchemyPasswordResetRepository:
    """Persistence for `PasswordReset`, backed by `identity_password_reset`. Hides its session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> PasswordResetId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return PasswordResetId(uuid7())

    async def add(self, reset: PasswordReset) -> None:
        """Insert a newly requested (addressed) reset. A plain flush: the id is a fresh UUIDv7 and an
        addressed reset has no token, so no constraint here is one a correct caller can hit. The
        mapping's `before_insert` hook writes `email`/`user_id` from `_target`."""
        self._session.add(reset)
        await self._session.flush()

    async def get(self, reset_id: PasswordResetId) -> PasswordReset | None:
        result = await self._session.execute(
            select(PasswordReset).where(_RESET_ID == reset_id)  # noqa: SIM300 -- column first
        )
        return result.scalar_one_or_none()

    async def find_by_token_hash(self, token_hash: TokenHash) -> PasswordReset | None:
        """The issued reset for `token_hash`, **unlocked** — `ResetPassword` needs only its `user_id`,
        to lock the user first (§0.8). Seeks `uq_identity_password_reset_token_hash`."""
        result = await self._session.execute(
            select(PasswordReset).where(
                _RESET_TOKEN_HASH == token_hash  # noqa: SIM300 -- column first
            )
        )
        return result.scalar_one_or_none()

    async def lock_by_token_hash(self, token_hash: TokenHash) -> PasswordReset | None:
        """`find_by_token_hash` under `FOR UPDATE`, with `populate_existing`.

        **`populate_existing` is not optional here**: `find_by_token_hash` loaded this very row into
        the identity map moments ago, and without it a row *changed* between the two reads would be
        handed back as it was first read. With it, the instance is overwritten from the row the lock
        was taken on, and the mapping's `refresh` hook rebuilds `_target` from it. `None` when the
        row went between the two reads (a concurrent confirm used it, a delivery superseded it, the
        sweep took it) — no row matches, so the stale instance is simply not returned.
        """
        result = await self._session.execute(
            select(PasswordReset)
            .where(_RESET_TOKEN_HASH == token_hash)  # noqa: SIM300 -- column first
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def save_issued(self, reset: PasswordReset) -> None:
        """Lock the account, persist the issue, then supersede the account's other resets — one
        unit, three statements.

        0. `SELECT 1 FROM identity_user WHERE id = :u FOR KEY SHARE` — **the user first** (§0.8).
           No row → `PasswordResetAlreadyIssued`: the account was erased while this delivery ran,
           and its erasure took this reset with it (by address) or is about to (the cascade), so
           there is nothing left to issue — the use case's `SKIPPED`, nothing sent.
        1. `UPDATE … SET email, user_id, token_hash, issued_at WHERE id = :id AND token_hash IS
           NULL` — the address cleared and the account set together (`target_columns`), which is what
           `ck_identity_password_reset_exactly_one_target` and `…_issued_with_account` demand of the
           one statement. 0 rows → `PasswordResetAlreadyIssued` (a concurrent delivery won, or the
           row went), **before** the `DELETE`, so a loser supersedes nothing.
        2. `DELETE … WHERE user_id = :u AND id <> :id` — one live reset link per account. It seeks
           `ix_identity_password_reset_user_id`. Addressed resets for the same address are *not*
           deleted: they have not been matched to an account yet, and their own delivery will
           supersede this one in turn (newest delivered wins).

        **Why step 0 exists** (`/verify` r1, `test_reset_delivery_vs_erasure_lock_order.py`): without
        it the `UPDATE` locks the reset row first and only then takes `FOR KEY SHARE` on the user,
        through the FK's RI trigger — reset, then user. Erasure holds the user `FOR UPDATE` and then
        deletes resets by address, reaching this very row — user, then reset. Opposite orders, so a
        cycle: `deadlock_detected`, reproduced 4/4. Taking the user's `FOR KEY SHARE` explicitly,
        before any reset row, makes delivery user-first like every other actor. `FOR KEY SHARE` is
        the weakest lock that conflicts with erasure's `FOR UPDATE`; it does not conflict with
        itself, so it costs a concurrent `LogIn` (`FOR SHARE`) nothing. Against a `ResetPassword`
        (user `FOR UPDATE`) or an erasure, delivery now **waits on the user** while holding no reset
        lock, and either proceeds once they commit or finds the user gone. The `UPDATE`'s own FK
        check then re-takes the lock this transaction already holds.

        **Expunged first**, for `SqlAlchemyPendingRegistrationRepository.save_issued`'s reason. Here
        it is doubly needed: the mapper does not see `_target` change, so a flush of the dirty
        instance would write `token_hash` with the stale `email` — refused by the CHECK as a 503.
        Superseded instances still in the identity map are expunged by the ids `RETURNING` names.
        """
        if reset in self._session:
            self._session.expunge(reset)
        email, user_id = target_columns(reset.target)
        account = await self._session.execute(
            # `read=True` as well: `key_share=True` alone renders `FOR NO KEY UPDATE` on PostgreSQL.
            select(user_table.c.id)
            .where(user_table.c.id == user_id)
            .with_for_update(read=True, key_share=True)
        )
        if account.first() is None:
            raise PasswordResetAlreadyIssued
        result = await self._session.execute(
            update(_table)
            .where(_table.c.id == reset.id, _table.c.token_hash.is_(None))
            .values(
                email=email,
                user_id=user_id,
                token_hash=reset.token_hash,
                issued_at=reset.issued_at,
            )
            .returning(_table.c.id)
        )
        if result.first() is None:
            raise PasswordResetAlreadyIssued
        superseded = await self._session.execute(
            delete(_table)
            .where(_table.c.user_id == user_id, _table.c.id != reset.id)
            .returning(_table.c.id)
        )
        self._expunge(superseded.scalars().all())

    async def remove(self, reset_id: PasswordResetId) -> None:
        """`DELETE … WHERE id = :id`. Idempotent. A stranded instance is expunged."""
        await self._session.execute(delete(_table).where(_table.c.id == reset_id))
        self._expunge((reset_id,))

    async def remove_all_for_user(self, user_id: UserId) -> int:
        """`DELETE … WHERE user_id = :u RETURNING id` — a used link spends every other link of the
        account (ADR-0028). Only *issued* resets have a `user_id`; an addressed one for the same
        address is untouched (it is matched to the account only at delivery). Returns the count;
        0 when there were none. Each removed instance leaves the identity map."""
        result = await self._session.execute(
            delete(_table).where(_table.c.user_id == user_id).returning(_table.c.id)
        )
        removed = result.scalars().all()
        self._expunge(removed)
        return len(removed)

    def _expunge(self, reset_ids: Iterable[PasswordResetId]) -> None:
        for reset_id in reset_ids:
            stale = self._session.identity_map.get(identity_key(PasswordReset, reset_id))
            if stale is not None:
                self._session.expunge(stale)


if TYPE_CHECKING:
    # Makes mypy prove the class structurally satisfies the port. Never executed.
    def _assert_implements_password_reset_repository(
        repo: SqlAlchemyPasswordResetRepository,
    ) -> None:
        _: PasswordResetRepository = repo
