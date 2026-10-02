"""`SqlAlchemyPendingRegistrationRepository` — the `PendingRegistrationRepository` port (ADR-0027,
technical plan §0.3, §0.4, §3).

Reads go through the ORM, so a query hands back a real aggregate. **Every write is Core** this module
wrote — the upsert, the guarded `UPDATE`, the `DELETE` — so each rule the port promises (newest wins,
issued once, idempotent removal) is a statement whose outcome this module reads, never a side effect
of a flush it did not ask for. 2.1's `SqlAlchemyLoginRepository` is the precedent.

**Core writes do not touch the identity map** (CLAUDE.md, 2.4's `d37481f`), so every write that can
leave a stale instance behind expunges it by identity. Filters query the private mapped attributes
through typed casts, for the reason the guest-session repository documents.

**Nothing here logs.** The row holds an address and a password hash; the one log line per delivery is
the worker's, built from the use case's outcome.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.util import identity_key

from tailorcraft.domain.identity.errors import PendingRegistrationAlreadyIssued
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.value_objects import PendingRegistrationId, TokenHash
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.mapping.identity.pending_registration import (
    pending_registration_table,
)

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import PendingRegistrationRepository

_PENDING_ID: InstrumentedAttribute[PendingRegistrationId] = cast(
    "InstrumentedAttribute[PendingRegistrationId]", PendingRegistration._id
)
_PENDING_TOKEN_HASH: InstrumentedAttribute[TokenHash | None] = cast(
    "InstrumentedAttribute[TokenHash | None]", PendingRegistration._token_hash
)

_table = pending_registration_table


class SqlAlchemyPendingRegistrationRepository:
    """Persistence for `PendingRegistration`, backed by `identity_pending_registration`.

    Hides its `AsyncSession` completely (ADR-0007). Nothing here commits; the request and delivery
    paths' durability ("durable on return") is `CommittingPendingRegistrationRepository`'s
    (`infrastructure/identity/token_access.py`).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> PendingRegistrationId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return PendingRegistrationId(uuid7())

    async def put(self, pending: PendingRegistration) -> None:
        """`INSERT … ON CONFLICT (email) DO UPDATE` — the supersede, in one statement (§0.3).

        **No read first.** Whether or not the address already had a pending row, the statement is the
        same, so the request path neither branches (a timing oracle) nor races a concurrent
        registration for the address (AC-17): the unique index `uq_identity_pending_registration_email`
        referees, and the later of two statements wins whole.

        On conflict the old row **becomes** the new one: new id, new password hash, new instants, and
        the token columns nulled — so a delivery task still queued for the old id finds nothing
        (`get` → `None`, `MISSING`) and a link already mailed for it stops resolving. The email is the
        conflict target and therefore unchanged.

        The aggregate is not added to the session: the row is written by Core, and the use case never
        reads `pending` back through this session. Any instance of the *old* row's id cannot be here
        either — the request path loads nothing.
        """
        values = {
            "id": pending.id,
            "email": pending.email,
            "password_hash": pending.password_hash,
            "requested_at": pending.requested_at,
            "expires_at": pending.expires_at,
            "token_hash": pending.token_hash,
            "issued_at": pending.issued_at,
        }
        statement = insert(_table).values(values)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[_table.c.email],
                set_={
                    "id": statement.excluded.id,
                    "password_hash": statement.excluded.password_hash,
                    "requested_at": statement.excluded.requested_at,
                    "expires_at": statement.excluded.expires_at,
                    "token_hash": None,
                    "issued_at": None,
                },
            )
        )

    async def get(self, pending_id: PendingRegistrationId) -> PendingRegistration | None:
        result = await self._session.execute(
            select(PendingRegistration).where(
                _PENDING_ID == pending_id  # noqa: SIM300 -- column first, see the casts
            )
        )
        return result.scalar_one_or_none()

    async def lock_by_token_hash(self, token_hash: TokenHash) -> PendingRegistration | None:
        """`SELECT … WHERE token_hash = :h FOR UPDATE`, seeking
        `uq_identity_pending_registration_token_hash`.

        **`populate_existing`**, for `SqlAlchemyUserRepository.get_for_update`'s reason: the lock is
        the point of the read, and an identity-map hit would otherwise hand back what this session
        read *before* it waited. Two clicks of one link queue here; once the first commits (its
        `remove` deleted the row), READ COMMITTED re-checks the locked row, finds it gone, and the
        second gets `None`.
        """
        result = await self._session.execute(
            select(PendingRegistration)
            .where(_PENDING_TOKEN_HASH == token_hash)  # noqa: SIM300 -- column first
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def save_issued(self, pending: PendingRegistration) -> None:
        """`UPDATE … SET token_hash, issued_at WHERE id = :id AND token_hash IS NULL`; 0 rows →
        `PendingRegistrationAlreadyIssued`.

        **The `token_hash IS NULL` predicate is the issued-once guard**, and it is in the statement,
        not in a read before it: two deliveries of one id that both loaded the row unissued both reach
        here, and under READ COMMITTED the second `UPDATE` waits for the first, re-evaluates its
        `WHERE` on the committed row, matches nothing, and raises — so only one mail is ever sent
        (AC-39). Zero rows also means the row went (superseded or removed meanwhile); the port has one
        answer for both, because in both cases this delivery must send nothing.

        **The aggregate is expunged first** (2.1's `save_rotation` lesson): `issue` dirtied it, and
        any autoflush — the `UPDATE`'s own `execute` triggers one — would push the ORM's `UPDATE`,
        which has no `token_hash IS NULL` predicate, ahead of this one and win the race the predicate
        exists to referee. Detached, it is never flushed; on success its new values are what the row
        now holds, and on `PendingRegistrationAlreadyIssued` the caller drops it.
        """
        if pending in self._session:
            self._session.expunge(pending)
        result = await self._session.execute(
            update(_table)
            .where(_table.c.id == pending.id, _table.c.token_hash.is_(None))
            .values(token_hash=pending.token_hash, issued_at=pending.issued_at)
            .returning(_table.c.id)
        )
        if result.first() is None:
            raise PendingRegistrationAlreadyIssued

    async def remove(self, pending_id: PendingRegistrationId) -> None:
        """`DELETE … WHERE id = :id`. Idempotent: deleting nothing is success.

        Any instance of this id still in the identity map is expunged, for
        `SqlAlchemyLoginRepository.remove`'s reason: a Core `DELETE` does not tell the ORM the row
        went, and a later flush of a dirty instance would target a row that no longer exists.
        """
        await self._session.execute(delete(_table).where(_table.c.id == pending_id))
        stale = self._session.identity_map.get(identity_key(PendingRegistration, pending_id))
        if stale is not None:
            self._session.expunge(stale)


if TYPE_CHECKING:
    # Makes mypy prove the class structurally satisfies the port. Never executed.
    def _assert_implements_pending_registration_repository(
        repo: SqlAlchemyPendingRegistrationRepository,
    ) -> None:
        _: PendingRegistrationRepository = repo
