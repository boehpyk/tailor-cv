"""`SqlAlchemyLoginRepository` — the `LoginRepository` port (ADR-0007, ADR-0020, technical plan §3).

Two tables, two styles, on purpose. `identity_login` is the mapped `Login` aggregate and is *read*
through the ORM, so a query hands back a real aggregate. `identity_retired_refresh_token` is an
unmapped Core `Table` — a lookup index, never an entity — and every write that has a concurrency
rule attached (`save_rotation`, `remove`, `remove_all`) is issued as **Core** SQL this module wrote,
so the rule is a statement whose outcome this module reads, not a side effect of a flush.

Filters query the private mapped attributes through typed casts, for the reason the guest-session
repository documents at length.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from tailorcraft.domain.identity.errors import LoginConcurrentlyRotated
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.value_objects import LoginId, RetiredRefreshToken, TokenHash
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.identity.login import (
    login_table,
    retired_refresh_token_table,
)

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import LoginRepository

# The retired index's primary key, named by `registry.py`'s convention. Its refusal is the second
# half of I-25: the same token retired twice means another rotation of this login already won.
_RETIRED_HASH_PK_CONSTRAINT: Final = "pk_identity_retired_refresh_token"

_LOGIN_ID: InstrumentedAttribute[LoginId] = cast("InstrumentedAttribute[LoginId]", Login._id)
_LOGIN_CURRENT_TOKEN_HASH: InstrumentedAttribute[TokenHash] = cast(
    "InstrumentedAttribute[TokenHash]", Login._current_token_hash
)

# The attributes a rotation changes, in the order `Login.rotate` changes them. `save_rotation`
# marks exactly these as persisted after its own `UPDATE` has written them.
_ROTATED_ATTRIBUTES: Final = ("_current_token_hash", "_generation", "_rotated_at")


class SqlAlchemyLoginRepository:
    """Persistence for `Login` and its append-only index of retired refresh tokens.

    Hides its `AsyncSession` completely (ADR-0007); the unit of work is the caller's.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> LoginId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return LoginId(uuid7())

    async def add(self, login: Login) -> None:
        """Insert a new login. A plain flush: no constraint here is one a correct caller can hit (the
        id is a fresh UUIDv7, the token hash a SHA-256 of 256 random bits), so a failure is a fault,
        not an outcome to translate."""
        self._session.add(login)
        await self._session.flush()

    async def find_by_current_token_hash(self, token_hash: TokenHash) -> Login | None:
        """One seek on `uq_identity_login_current_token_hash`."""
        result = await self._session.execute(
            select(Login).where(_LOGIN_CURRENT_TOKEN_HASH == token_hash)  # noqa: SIM300 -- column first, see the casts
        )
        return result.scalar_one_or_none()

    async def find_by_retired_token_hash(self, token_hash: TokenHash) -> tuple[Login, int] | None:
        """One seek on the retired index's primary key, joined to the login that issued the token.

        A join rather than two queries so the pair is read from one snapshot: a login revoked between
        two separate reads would otherwise hand back a generation for a login that no longer exists.
        If the login is gone, the cascade took its retired rows with it, and the answer is `None` —
        "unknown", which after a revocation is the correct answer (ADR-0020).
        """
        retired = retired_refresh_token_table
        result = await self._session.execute(
            select(Login, retired.c.generation)
            .join(retired, retired.c.login_id == _LOGIN_ID)
            .where(retired.c.token_hash == token_hash)
        )
        row = result.one_or_none()
        if row is None:
            return None
        login, generation = row
        return login, generation

    async def save_rotation(self, login: Login, retired: RetiredRefreshToken) -> None:
        """Persist a rotation as one unit, or raise `LoginConcurrentlyRotated` and change nothing.

        **Two statements, both Core, inside one SAVEPOINT:**

        1. `UPDATE identity_login SET current_token_hash, generation, rotated_at,
           version = version + 1 WHERE id = :id AND version = :loaded RETURNING version`. No row back
           means the version moved since this login was loaded — another request rotated it first.
           Under READ COMMITTED the loser's `UPDATE` *waits* on the winner's row lock and then
           re-evaluates its `WHERE` against the committed row, so the check is atomic, not a
           read-then-write race (AC-17).
        2. `INSERT` the retired hash. Its primary key refusing (`pk_identity_retired_refresh_token`,
           recognised by name) means this exact token was already retired — the same verdict. Any
           other refusal is a fault and propagates.

        The SAVEPOINT is what lets `RefreshLogin` answer 409 with a transaction that still commits:
        a refusal rolls back to it, not to the request's root (CLAUDE.md, the 1.4 lesson).

        **The identity map — why the login leaves the session first.** `login` was loaded through the
        ORM and `Login.rotate` has since changed three of its mapped attributes, so it is *dirty*.
        Any flush — and `begin_nested()` flushes unconditionally on entry — would emit the ORM's own
        `UPDATE identity_login … WHERE id = :id` with **no** version predicate, writing the rotation
        past the very check step 1 exists to make; the loser of a race would overwrite the winner's
        current token and both requests would answer 200. `version` is deliberately not the mapper's
        `version_id_col` (the mapping's docstring says why), so nothing else stands in the way.
        Hence `expunge` before the SAVEPOINT: the ORM forgets the object and can never flush it, and
        the only `UPDATE` that reaches the table is the one written below.

        Rejected alternatives, recorded so nobody re-derives them:
        - *Run the update in Core, then `refresh()` the object.* The dirty object would still be
          flushed by `begin_nested()`'s entry flush before the Core statement ran.
        - *Mark the attributes committed before the SAVEPOINT, keeping it attached.* On a refusal the
          identity map would then hold a `Login` claiming a rotation the database never saw, and a
          later read in the same session would be handed that fiction instead of the row.

        **On success** the object is re-attached **clean**: the three rotated attributes and the new
        `version` are marked as persisted (`set_committed_value`), which is the truth — step 1 wrote
        exactly those values — so a later flush has nothing to write and a later read in this session
        returns the up-to-date aggregate. Measured, not assumed: the statements this method sends
        are exactly `SAVEPOINT`, the `UPDATE`, the `INSERT` and `RELEASE`, and a flush afterwards
        sends nothing. `session.dirty` may still *list* the object — `set_committed_value` clears
        each attribute's history but not the instance's `modified` flag — which is why the proof
        is `session.is_modified(login)` being `False`, not the `dirty` set. **On a refusal** it stays detached and still carries the
        rotation it failed to persist; the caller discards it (`RefreshLogin` answers
        `RefreshInProgress` and touches it no further). A login that was never attached to this
        session is not attached by this method either — adding a transient object would queue an
        `INSERT`.

        Every value is read into a local before the first statement, so nothing below touches the
        ORM object while a statement is in flight or after a rollback.
        """
        login_id = login.id
        loaded_version = login.version
        was_attached = login in self._session
        if was_attached:
            self._session.expunge(login)

        try:
            async with self._session.begin_nested():
                new_version = (
                    await self._session.execute(
                        update(login_table)
                        .where(
                            login_table.c.id == login_id, login_table.c.version == loaded_version
                        )
                        .values(
                            current_token_hash=login.current_token_hash,
                            generation=login.generation,
                            rotated_at=login.rotated_at,
                            version=login_table.c.version + 1,
                        )
                        .returning(login_table.c.version)
                    )
                ).scalar_one_or_none()
                if new_version is None:
                    # Raised inside the `async with`, so the SAVEPOINT rolls back — there is nothing
                    # to undo, but the boundary stays symmetrical with the `INSERT`'s refusal.
                    raise LoginConcurrentlyRotated()
                await self._session.execute(
                    insert(retired_refresh_token_table).values(
                        token_hash=retired.token_hash,
                        login_id=login_id,
                        generation=retired.generation,
                        retired_at=retired.retired_at,
                    )
                )
        except IntegrityError as exc:
            if violated_constraint(exc) == _RETIRED_HASH_PK_CONSTRAINT:
                raise LoginConcurrentlyRotated() from None
            raise

        for attribute in _ROTATED_ATTRIBUTES:
            set_committed_value(login, attribute, getattr(login, attribute))
        set_committed_value(login, "_version", new_version)
        if was_attached:
            self._session.add(login)

    async def remove(self, login_id: LoginId) -> None:
        """`DELETE` the login; its retired hashes go by `ON DELETE CASCADE`. Idempotent — deleting
        nothing is success (AC-11).

        A Core statement, so the ORM does not know the row went: any `Login` for this id still in the
        identity map is expunged, or a later flush of it (dirty from a `rotate` that was refused as
        expired, say) would `UPDATE` a row that no longer exists and raise `StaleDataError` into a
        request that had already succeeded.
        """
        await self._session.execute(delete(login_table).where(login_table.c.id == login_id))
        stale = self._session.identity_map.get(identity_key(Login, login_id))
        if stale is not None:
            self._session.expunge(stale)

    async def count_all(self) -> int:
        """`SELECT count(*)` — what the break-glass's dry run reports (AC-13)."""
        result = await self._session.execute(select(func.count()).select_from(login_table))
        return result.scalar_one()

    async def remove_all(self) -> int:
        """`DELETE` every login — the break-glass (AC-13). Returns how many rows went; 0 on an empty
        table.

        The count is the statement's own row count, read on the session's connection (whose
        `execute` returns a `CursorResult`), rather than a `count(*)` beforehand: a login created
        between a count and the delete would be deleted and not counted. Every `Login` in the
        identity map is then expunged, for the reason `remove` gives.
        """
        connection = await self._session.connection()
        result = await connection.execute(delete(login_table))
        for instance in list(self._session.identity_map.values()):
            if isinstance(instance, Login):
                self._session.expunge(instance)
        return result.rowcount


if TYPE_CHECKING:
    # Makes mypy prove the class structurally satisfies the port. Never executed.
    def _assert_implements_login_repository(repo: SqlAlchemyLoginRepository) -> None:
        _: LoginRepository = repo
