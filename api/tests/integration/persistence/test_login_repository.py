"""Persistence tests for `Login` — the imperative mapping, `identity_retired_refresh_token` (an
unmapped Core `Table`), and `SqlAlchemyLoginRepository` against real PostgreSQL (T22, written
**after**).

**Two tables, two styles, exactly as `mapping/identity/login.py`'s module docstring describes.**
`identity_login` is read through the ORM (`find_by_current_token_hash`); every write with a
concurrency rule attached (`save_rotation`, `remove`, `remove_all`) is Core SQL this repository wrote,
because the rule has to be a statement whose outcome the repository reads, not a side effect of a
flush it did not ask for (CLAUDE.md's 1.4 lesson, the reason `version` is not the mapper's
`version_id_col`).

**`save_rotation`'s three outcomes each need a different kind of concurrency to reach, and this file
reaches each one differently:**

- *Success* — an ordinary rotation.
- *A stale version* — simulated with a raw `UPDATE` that bumps `identity_login.version` behind the
  loaded `Login`'s back, standing in for a second session's already-committed rotation (the
  technique the implementer's notes name: "simulate a concurrent rotation by bumping the row's
  version with raw SQL").
- *A primary-key collision on the retired hash* — a raw `INSERT` into
  `identity_retired_refresh_token` at exactly the hash the coming rotation is about to retire, the
  same technique `test_tailoring_run_repository.py`'s CHECK-constraint tests use to reach a state the
  aggregate itself cannot construct.

Both outcomes prove `session.is_modified(login)` and direct table reads, never trust the exception
alone — an implementation that raised `LoginConcurrentlyRotated` for the wrong reason, or left a
half-written row behind, would still pass a test that only checks the exception type.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.errors import LoginConcurrentlyRotated
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, TokenHash, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.identity.login import (
    login_table,
    retired_refresh_token_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.repositories.identity.login import (
    SqlAlchemyLoginRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)

_LIFETIME = timedelta(days=30)


async def _persist_user(session: AsyncSession, clock: FixedClock) -> UserId:
    """A `Login` always needs a real owner: `identity_login.user_id` is `NOT NULL` with a foreign
    key to `identity_user.id`. The email is keyed off the fresh user id so repeated calls within one
    test's transaction never collide on the unique index."""
    users = SqlAlchemyUserRepository(session)
    user_id = users.next_identity()
    await users.add(
        User.register_with_password(
            id=user_id,
            email=EmailAddress.parse(f"user-{user_id.value}@example.com"),
            password_hash=PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"),
            at=clock.now(),
        )
    )
    return user_id


# --- Round trip, before and after a rotation ----------------------------------------------------


async def test_round_trip_before_and_after_a_rotation_preserves_whole_second_timestamps(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock)
    logins = SqlAlchemyLoginRepository(session)
    login_id = logins.next_identity()
    first_hash = TokenHash("1" * 64)
    login = Login.start(
        id=login_id, user_id=user_id, token_hash=first_hash, at=clock.now(), lifetime=_LIFETIME
    )
    await logins.add(login)

    session.expunge_all()
    reloaded = await logins.find_by_current_token_hash(first_hash)
    assert reloaded is not None
    assert reloaded.id == login_id
    assert reloaded.user_id == user_id
    assert reloaded.generation == 1
    assert reloaded.rotated_at is None
    assert reloaded.version == 1
    assert reloaded.created_at == login.created_at
    assert reloaded.expires_at == login.expires_at
    assert reloaded.created_at.microsecond == 0
    assert reloaded.expires_at.microsecond == 0

    session.expunge_all()
    live = await logins.find_by_current_token_hash(first_hash)
    assert live is not None
    second_hash = TokenHash("2" * 64)
    rotation_instant = clock.now() + timedelta(minutes=1)
    retired = live.rotate(second_hash, rotation_instant)
    await logins.save_rotation(live, retired)

    session.expunge_all()
    after_rotation = await logins.find_by_current_token_hash(second_hash)
    assert after_rotation is not None
    assert after_rotation.id == login_id
    assert after_rotation.generation == 2
    assert after_rotation.version == 2
    assert after_rotation.current_token_hash == second_hash
    assert after_rotation.rotated_at == rotation_instant
    assert after_rotation.rotated_at is not None
    assert after_rotation.rotated_at.microsecond == 0

    found = await logins.find_by_retired_token_hash(first_hash)
    assert found is not None
    retired_login, generation = found
    assert generation == 1
    assert retired_login.id == login_id


async def test_find_by_current_token_hash_returns_none_for_an_unknown_hash(
    session: AsyncSession,
) -> None:
    logins = SqlAlchemyLoginRepository(session)
    assert await logins.find_by_current_token_hash(TokenHash("3" * 64)) is None


async def test_find_by_retired_token_hash_returns_none_for_an_unknown_hash(
    session: AsyncSession,
) -> None:
    logins = SqlAlchemyLoginRepository(session)
    assert await logins.find_by_retired_token_hash(TokenHash("4" * 64)) is None


# --- save_rotation: a stale version --------------------------------------------------------------


async def test_save_rotation_refuses_a_stale_version_and_leaves_the_session_usable(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock)
    logins = SqlAlchemyLoginRepository(session)
    login = Login.start(
        id=logins.next_identity(),
        user_id=user_id,
        token_hash=TokenHash("5" * 64),
        at=clock.now(),
        lifetime=_LIFETIME,
    )
    await logins.add(login)

    # A second session's rotation that already committed, bumping `version` behind this `Login`
    # object's back — a raw `UPDATE`, never through the repository, standing in for the concurrent
    # writer.
    await session.execute(
        update(login_table)
        .where(login_table.c.id == login.id)
        .values(version=login_table.c.version + 1)
    )

    retired = login.rotate(TokenHash("6" * 64), clock.now())

    with pytest.raises(LoginConcurrentlyRotated):
        await logins.save_rotation(login, retired)

    # On a refusal the login stays DETACHED but still carries the rotation it failed to persist
    # (the repository's own docstring) — `rotate()` already mutated its attributes in memory, and
    # nothing committed them, so `is_modified` is correctly True here. False belongs to the SUCCESS
    # path only (see the dedicated test below), where the rotated attributes are marked committed.
    assert session.is_modified(login) is True
    assert login not in session, "a refused rotation must not leave the login re-attached"

    # The SAVEPOINT contains the refusal — another write succeeds in the same transaction.
    other = Login.start(
        id=logins.next_identity(),
        user_id=user_id,
        token_hash=TokenHash("7" * 64),
        at=clock.now(),
        lifetime=_LIFETIME,
    )
    await logins.add(other)

    # The database row is exactly what the raw bump left it at — the refused rotation wrote nothing.
    row = (
        await session.execute(
            select(
                login_table.c.current_token_hash, login_table.c.version, login_table.c.generation
            ).where(login_table.c.id == login.id)
        )
    ).one()
    assert row.current_token_hash == TokenHash("5" * 64)
    assert row.version == 2
    assert row.generation == 1


# --- save_rotation: success ----------------------------------------------------------------------


async def test_save_rotation_leaves_the_login_not_modified_after_success(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock)
    logins = SqlAlchemyLoginRepository(session)
    login = Login.start(
        id=logins.next_identity(),
        user_id=user_id,
        token_hash=TokenHash("8" * 64),
        at=clock.now(),
        lifetime=_LIFETIME,
    )
    await logins.add(login)

    retired = login.rotate(TokenHash("9" * 64), clock.now())
    await logins.save_rotation(login, retired)

    # `set_committed_value` clears each rotated attribute's history but not the instance's
    # `modified` flag — `session.dirty` would still list it. `is_modified` is the repository's own
    # documented proof, not `session.dirty` (the implementer's note).
    assert session.is_modified(login) is False

    # A flush right after issues no further SQL: nothing about the successful rotation is left
    # pending.
    await session.flush()

    session.expunge_all()
    reloaded = await logins.find_by_current_token_hash(TokenHash("9" * 64))
    assert reloaded is not None
    assert reloaded.generation == 2
    assert reloaded.version == 2


# --- save_rotation: a primary-key collision on the retired hash ---------------------------------


async def test_save_rotation_refuses_a_primary_key_collision_on_the_retired_hash(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock)
    logins = SqlAlchemyLoginRepository(session)
    current_hash = TokenHash("a1" * 32)
    login = Login.start(
        id=logins.next_identity(),
        user_id=user_id,
        token_hash=current_hash,
        at=clock.now(),
        lifetime=_LIFETIME,
    )
    await logins.add(login)

    # A row already retired at exactly the hash the coming rotation is about to retire — the second
    # half of I-25, the same token retired twice because a concurrent rotation already won.
    await session.execute(
        insert(retired_refresh_token_table).values(
            token_hash=current_hash, login_id=login.id, generation=1, retired_at=clock.now()
        )
    )

    retired = login.rotate(TokenHash("b2" * 32), clock.now())
    assert retired.token_hash == current_hash

    with pytest.raises(LoginConcurrentlyRotated):
        await logins.save_rotation(login, retired)

    # As above: a refusal leaves the login detached but still carrying its attempted rotation.
    assert session.is_modified(login) is True
    assert login not in session

    count = (
        await session.execute(
            select(func.count())
            .select_from(retired_refresh_token_table)
            .where(retired_refresh_token_table.c.token_hash == current_hash)
        )
    ).scalar_one()
    assert count == 1, "the SAVEPOINT must have rolled back the colliding INSERT, not doubled it"

    row = (
        await session.execute(
            select(login_table.c.generation, login_table.c.current_token_hash).where(
                login_table.c.id == login.id
            )
        )
    ).one()
    assert row.generation == 1, "the refused rotation must not have advanced the generation"
    assert row.current_token_hash == current_hash


# --- remove: idempotent, and its cascades --------------------------------------------------------


async def test_remove_is_idempotent_when_called_twice(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock)
    logins = SqlAlchemyLoginRepository(session)
    login = Login.start(
        id=logins.next_identity(),
        user_id=user_id,
        token_hash=TokenHash("c3" * 32),
        at=clock.now(),
        lifetime=_LIFETIME,
    )
    await logins.add(login)

    await logins.remove(login.id)
    await logins.remove(login.id)  # a second removal of the same, already-gone id is not an error

    remaining = await session.execute(select(login_table.c.id).where(login_table.c.id == login.id))
    assert remaining.scalar_one_or_none() is None


async def test_remove_of_an_unknown_login_id_is_a_no_op(session: AsyncSession) -> None:
    logins = SqlAlchemyLoginRepository(session)
    await logins.remove(logins.next_identity())  # no row exists at all — still not an error


async def test_removing_a_login_cascades_to_its_retired_hashes(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock)
    logins = SqlAlchemyLoginRepository(session)
    login = Login.start(
        id=logins.next_identity(),
        user_id=user_id,
        token_hash=TokenHash("d4" * 32),
        at=clock.now(),
        lifetime=_LIFETIME,
    )
    await logins.add(login)

    retired = login.rotate(TokenHash("e5" * 32), clock.now())
    await logins.save_rotation(login, retired)

    before = (
        await session.execute(
            select(func.count())
            .select_from(retired_refresh_token_table)
            .where(retired_refresh_token_table.c.login_id == login.id)
        )
    ).scalar_one()
    assert before == 1

    await logins.remove(login.id)

    after = (
        await session.execute(
            select(func.count())
            .select_from(retired_refresh_token_table)
            .where(retired_refresh_token_table.c.login_id == login.id)
        )
    ).scalar_one()
    assert after == 0


async def test_deleting_a_user_row_cascades_to_its_logins(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock)
    logins = SqlAlchemyLoginRepository(session)
    login = Login.start(
        id=logins.next_identity(),
        user_id=user_id,
        token_hash=TokenHash("f6" * 32),
        at=clock.now(),
        lifetime=_LIFETIME,
    )
    await logins.add(login)

    await session.execute(user_table.delete().where(user_table.c.id == user_id))
    await session.flush()

    remaining = await session.execute(select(login_table.c.id).where(login_table.c.id == login.id))
    assert remaining.scalar_one_or_none() is None


# --- remove_all / count_all -----------------------------------------------------------------------


async def test_remove_all_and_count_all(session: AsyncSession, clock: FixedClock) -> None:
    user_id = await _persist_user(session, clock)
    logins = SqlAlchemyLoginRepository(session)

    assert await logins.count_all() == 0

    for index in range(3):
        login = Login.start(
            id=logins.next_identity(),
            user_id=user_id,
            token_hash=TokenHash(f"{index}7" * 32),
            at=clock.now(),
            lifetime=_LIFETIME,
        )
        await logins.add(login)

    assert await logins.count_all() == 3

    removed = await logins.remove_all()
    assert removed == 3
    assert await logins.count_all() == 0


async def test_remove_all_on_an_empty_table_returns_zero(session: AsyncSession) -> None:
    logins = SqlAlchemyLoginRepository(session)
    assert await logins.remove_all() == 0
