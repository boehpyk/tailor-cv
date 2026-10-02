"""The repository methods slice 2.5 added to `User` and `Login` persistence (T23, test-after):
`SqlAlchemyUserRepository.get_for_update`, `confirm_credential_unchanged` and
`SqlAlchemyLoginRepository.remove_all_for_user` (plan §0.7, §0.8; AC-19).

**Statement capture** (`before_cursor_execute` on the fixture's connection) proves the SQL that
reached the driver, because the locking modes are the entire point of two of these methods: a
`FOR SHARE` that silently became a plain `SELECT` would still return the right boolean and would
defeat the login-versus-reset ordering. **Behavioural lock tests** on two pinned real connections then
prove the locks do what the plan says they do: a held `FOR SHARE` makes a reset's `FOR UPDATE` wait,
and a `FOR UPDATE` serialises two writers. Each overlap is observed in `pg_stat_activity`
(`wait_for_lock_waiter`), never assumed from a sleep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

# Imported for its side effect (`map_imperatively`); see test_pending_registration_repository.py.
import tailorcraft.infrastructure.persistence.retention.account_data  # noqa: F401
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.value_objects import PasswordHash, TokenHash, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.identity.login import (
    SqlAlchemyLoginRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tests.integration.claim_race_support import (
    assert_test_database,
    new_user,
    pinned_session,
    wait_for_lock_waiter,
)
from tests.integration.persistence.owner_rows import persist_user

_LIFETIME = timedelta(days=30)
_STEP_TIMEOUT = 15.0
_PHC_SEED = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"  # `owner_rows`' hash
_OTHER_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$b3RoZXI")


@contextmanager
def _captured_statements(connection: AsyncConnection) -> Iterator[list[str]]:
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    event.listen(connection.sync_connection, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", _record)


# ------------------------------------------------------------------------------ get_for_update


async def test_get_for_update_returns_the_user_under_a_for_update_lock(
    session: AsyncSession, connection: AsyncConnection, clock: FixedClock
) -> None:
    users = SqlAlchemyUserRepository(session)
    owner = await persist_user(session, clock)
    session.expunge_all()

    with _captured_statements(connection) as statements:
        user = await users.get_for_update(owner.user_id)

    assert user.id == owner.user_id
    (select_statement,) = [s for s in statements if "identity_user" in s]
    assert select_statement.rstrip().upper().endswith("FOR UPDATE")


async def test_get_for_update_of_an_unknown_user_is_user_not_found(
    session: AsyncSession,
) -> None:
    users = SqlAlchemyUserRepository(session)

    with pytest.raises(UserNotFound):
        await users.get_for_update(UserId(uuid4()))


async def test_get_for_update_overwrites_an_instance_loaded_before_the_lock(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`populate_existing`: the user was read, a concurrent writer changed the hash, and the locked
    read must act on the hash as it is *now* (a reset acting on a replaced hash would overwrite a
    newer password)."""
    users = SqlAlchemyUserRepository(session)
    owner = await persist_user(session, clock)
    session.expunge_all()
    before = await users.get(owner.user_id)
    assert before is not None
    await session.execute(
        text("UPDATE identity_user SET password_hash = :h WHERE id = :i"),
        {"h": _OTHER_HASH.value, "i": owner.user_id.value},
    )

    locked = await users.get_for_update(owner.user_id)

    assert locked.password_hash == _OTHER_HASH


async def test_a_second_get_for_update_waits_for_the_first_transaction(
    engine: AsyncEngine, clock: FixedClock, settings: Any
) -> None:
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    try:
        async with pinned_session(engine) as a, pinned_session(engine) as b:
            held = await SqlAlchemyUserRepository(a).get_for_update(user_id)
            assert held.id == user_id

            waiting = asyncio.create_task(SqlAlchemyUserRepository(b).get_for_update(user_id))
            await wait_for_lock_waiter(engine, "identity_user", "for update")
            assert not waiting.done()
            await a.commit()

            assert (await asyncio.wait_for(waiting, _STEP_TIMEOUT)).id == user_id
            await b.rollback()
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id.value}
            )


# ------------------------------------------------------------------ confirm_credential_unchanged


async def test_confirm_credential_unchanged_is_true_for_the_hash_the_row_holds(
    session: AsyncSession, clock: FixedClock
) -> None:
    users = SqlAlchemyUserRepository(session)
    owner = await persist_user(session, clock)

    assert await users.confirm_credential_unchanged(owner.user_id, PasswordHash(_PHC_SEED)) is True


async def test_confirm_credential_unchanged_is_false_for_any_other_hash(
    session: AsyncSession, clock: FixedClock
) -> None:
    users = SqlAlchemyUserRepository(session)
    owner = await persist_user(session, clock)

    assert await users.confirm_credential_unchanged(owner.user_id, _OTHER_HASH) is False


async def test_confirm_credential_unchanged_is_false_once_the_hash_was_replaced_underneath(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The answer comes from the row as it is now, never from the instance this session loaded
    before verifying (the very value being checked)."""
    users = SqlAlchemyUserRepository(session)
    owner = await persist_user(session, clock)
    seen = (await users.get(owner.user_id)).password_hash
    await session.execute(
        text("UPDATE identity_user SET password_hash = :h WHERE id = :i"),
        {"h": _OTHER_HASH.value, "i": owner.user_id.value},
    )

    assert await users.confirm_credential_unchanged(owner.user_id, seen) is False


async def test_confirm_credential_unchanged_is_false_for_a_user_that_is_gone(
    session: AsyncSession,
) -> None:
    users = SqlAlchemyUserRepository(session)

    assert (
        await users.confirm_credential_unchanged(UserId(uuid4()), PasswordHash(_PHC_SEED)) is False
    )


async def test_confirm_credential_unchanged_is_a_for_share_select_with_the_hash_in_its_where(
    session: AsyncSession, connection: AsyncConnection, clock: FixedClock
) -> None:
    """AC-19: `SELECT … FOR SHARE`, with the hash compared in SQL (so a concurrent reset's committed
    row is what the lock re-evaluates), and not `FOR UPDATE` (two concurrent logins must not
    serialise on each other)."""
    users = SqlAlchemyUserRepository(session)
    owner = await persist_user(session, clock)

    with _captured_statements(connection) as statements:
        await users.confirm_credential_unchanged(owner.user_id, PasswordHash(_PHC_SEED))

    (statement,) = [s for s in statements if "identity_user" in s]
    upper = statement.upper()
    assert upper.rstrip().endswith("FOR SHARE")
    assert "FOR UPDATE" not in upper
    where = statement.split("WHERE", 1)[1].lower()
    assert "password_hash" in where
    assert "identity_user.id" in where


async def test_a_held_share_lock_makes_a_reset_wait_and_then_see_the_login(
    engine: AsyncEngine, clock: FixedClock, settings: Any
) -> None:
    """The ordering the plan relies on (§0.7): a login that confirmed the credential holds `FOR SHARE`
    until it commits, so a reset's `get_for_update` waits for it — and the reset then deletes the
    `Login` the login wrote."""
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    try:
        async with pinned_session(engine) as login_tx, pinned_session(engine) as reset_tx:
            assert await SqlAlchemyUserRepository(login_tx).confirm_credential_unchanged(
                user_id, PasswordHash(_PHC_SEED)
            )

            reset = asyncio.create_task(SqlAlchemyUserRepository(reset_tx).get_for_update(user_id))
            await wait_for_lock_waiter(engine, "identity_user", "for update")
            assert not reset.done(), "the reset must wait for the login's shared lock"
            await login_tx.commit()

            assert (await asyncio.wait_for(reset, _STEP_TIMEOUT)).id == user_id
            await reset_tx.rollback()
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id.value}
            )


async def test_two_logins_confirming_at_once_do_not_wait_for_each_other(
    engine: AsyncEngine, clock: FixedClock, settings: Any
) -> None:
    """`FOR SHARE` is shared: the second confirmation answers while the first still holds its lock
    (a `FOR UPDATE` here would make concurrent logins of one user queue)."""
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    try:
        async with pinned_session(engine) as first, pinned_session(engine) as second:
            assert await SqlAlchemyUserRepository(first).confirm_credential_unchanged(
                user_id, PasswordHash(_PHC_SEED)
            )
            answered = await asyncio.wait_for(
                SqlAlchemyUserRepository(second).confirm_credential_unchanged(
                    user_id, PasswordHash(_PHC_SEED)
                ),
                _STEP_TIMEOUT,
            )
            assert answered is True
            await first.rollback()
            await second.rollback()
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id.value}
            )


# --------------------------------------------------------------------- remove_all_for_user (logins)


async def _login(
    logins: SqlAlchemyLoginRepository, clock: FixedClock, user_id: UserId, char: str
) -> Login:
    login = Login.start(
        id=logins.next_identity(),
        user_id=user_id,
        token_hash=TokenHash(char * 64),
        at=clock.now(),
        lifetime=_LIFETIME,
    )
    await logins.add(login)
    return login


async def _login_ids(session: AsyncSession, user_id: UserId) -> set[Any]:
    rows = await session.execute(
        text("SELECT id FROM identity_login WHERE user_id = :u"), {"u": user_id.value}
    )
    return {r[0] for r in rows.all()}


async def test_remove_all_for_user_deletes_every_login_of_that_user_and_returns_the_count(
    session: AsyncSession, clock: FixedClock
) -> None:
    logins = SqlAlchemyLoginRepository(session)
    mine = await persist_user(session, clock)
    for char in "123":
        await _login(logins, clock, mine.user_id, char)

    removed = await logins.remove_all_for_user(mine.user_id)

    assert removed == 3
    assert await _login_ids(session, mine.user_id) == set()


async def test_remove_all_for_user_leaves_other_users_logins_alone(
    session: AsyncSession, clock: FixedClock
) -> None:
    logins = SqlAlchemyLoginRepository(session)
    mine = await persist_user(session, clock)
    theirs = await persist_user(session, clock)
    await _login(logins, clock, mine.user_id, "1")
    survivor = await _login(logins, clock, theirs.user_id, "2")

    assert await logins.remove_all_for_user(mine.user_id) == 1

    assert await _login_ids(session, theirs.user_id) == {survivor.id.value}


async def test_remove_all_for_a_user_with_no_logins_is_zero(
    session: AsyncSession, clock: FixedClock
) -> None:
    logins = SqlAlchemyLoginRepository(session)
    owner = await persist_user(session, clock)

    assert await logins.remove_all_for_user(owner.user_id) == 0


async def test_remove_all_for_user_takes_the_retired_hashes_with_their_logins(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Revocation is deletion (ADR-0020): the retired-hash rows go by the `login_id` cascade, so a
    stolen old refresh token is not even recognised as reuse afterwards."""
    logins = SqlAlchemyLoginRepository(session)
    owner = await persist_user(session, clock)
    login = await _login(logins, clock, owner.user_id, "1")
    session.expunge_all()
    live = await logins.find_by_current_token_hash(TokenHash("1" * 64))
    assert live is not None
    retired = live.rotate(TokenHash("2" * 64), clock.now() + timedelta(minutes=1))
    await logins.save_rotation(live, retired)
    before = (
        await session.execute(
            text("SELECT count(*) FROM identity_retired_refresh_token WHERE login_id = :l"),
            {"l": login.id.value},
        )
    ).scalar_one()
    assert before == 1

    await logins.remove_all_for_user(owner.user_id)

    after = (
        await session.execute(
            text("SELECT count(*) FROM identity_retired_refresh_token WHERE login_id = :l"),
            {"l": login.id.value},
        )
    ).scalar_one()
    assert after == 0


async def test_remove_all_for_user_expunges_the_logins_it_deleted(
    session: AsyncSession, clock: FixedClock
) -> None:
    logins = SqlAlchemyLoginRepository(session)
    owner = await persist_user(session, clock)
    await _login(logins, clock, owner.user_id, "1")
    session.expunge_all()
    loaded = await logins.find_by_current_token_hash(TokenHash("1" * 64))
    assert loaded is not None
    assert loaded in session

    await logins.remove_all_for_user(owner.user_id)

    assert loaded not in session
