"""`SqlAlchemyPasswordResetRepository` against real PostgreSQL (slice 2.5, T23, test-after; plan §0.4,
§0.7, §0.8 and AC-18/AC-19's reset halves).

`ResetTarget` is the interesting part: the aggregate holds one attribute (`AddressedReset` or
`IssuedReset`) and the row two nullable columns, translated in the mapping alone. The round trip is
asserted **both ways** (aggregate to row, row to aggregate), including the case the mapper cannot
see — `PasswordReset.issue` changes only `_target`, so the repository's Core `UPDATE` must write the
two columns itself — and the case where a row changes underneath an instance the session already
holds (`populate_existing`, which fires the mapping's `refresh` hook, not `load`).

Same three styles as `test_pending_registration_repository.py`: rolled-back `session` tests for shape
and sequences, two pinned real connections for the race (staged from `pg_stat_activity`), and
committed seeds scoped to their own ids and deleted afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

# Imported for its side effect (`map_imperatively`), as in the pending-registration file.
import tailorcraft.infrastructure.persistence.retention.account_data  # noqa: F401
from tailorcraft.domain.identity.errors import PasswordResetAlreadyIssued
from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.value_objects import (
    AddressedReset,
    EmailAddress,
    IssuedReset,
    PasswordResetId,
    TokenHash,
    UserId,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.identity.password_reset import (
    SqlAlchemyPasswordResetRepository,
)
from tests.integration.claim_race_support import (
    assert_test_database,
    new_user,
    pinned_session,
    wait_for_lock_waiter,
)
from tests.integration.persistence.owner_rows import persist_user

_TTL = timedelta(hours=1)
_STEP_TIMEOUT = 15.0


def _hash(char: str) -> TokenHash:
    return TokenHash(char * 64)


def _address() -> EmailAddress:
    return EmailAddress.parse(f"reset-{uuid4().hex}@example.com")


def _request(
    repo: SqlAlchemyPasswordResetRepository, clock: FixedClock, email: EmailAddress
) -> PasswordReset:
    return PasswordReset.request(id=repo.next_identity(), email=email, at=clock.now(), ttl=_TTL)


async def _row(session: AsyncSession, reset_id: PasswordResetId) -> Any:
    return (
        await session.execute(
            text(
                "SELECT email, user_id, token_hash, issued_at FROM identity_password_reset "
                "WHERE id = :i"
            ),
            {"i": reset_id.value},
        )
    ).one_or_none()


async def _ids_of(session: AsyncSession, user_id: UserId) -> set[Any]:
    rows = await session.execute(
        text("SELECT id FROM identity_password_reset WHERE user_id = :u"), {"u": user_id.value}
    )
    return {r[0] for r in rows.all()}


async def _issued_for(
    session: AsyncSession,
    repo: SqlAlchemyPasswordResetRepository,
    clock: FixedClock,
    user_id: UserId,
    token_hash: TokenHash,
) -> PasswordReset:
    """A reset requested and then issued to `user_id`, through the repository."""
    reset = _request(repo, clock, _address())
    await repo.add(reset)
    session.expunge_all()
    loaded = await repo.get(reset.id)
    assert loaded is not None
    loaded.issue(user_id, token_hash, clock.now())
    await repo.save_issued(loaded)
    return loaded


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


# ----------------------------------------------------------------------------- the target mapping


async def test_an_addressed_reset_round_trips_and_writes_only_the_email_column(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)
    email = _address()
    reset = _request(repo, clock, email)

    await repo.add(reset)

    stored = await _row(session, reset.id)
    assert (stored.email, stored.user_id, stored.token_hash, stored.issued_at) == (
        email.value,
        None,
        None,
        None,
    )
    session.expunge_all()
    found = await repo.get(reset.id)
    assert found is not None
    assert found.target == AddressedReset(email)
    assert found.token_hash is None
    assert found.requested_at == reset.requested_at
    assert found.expires_at == reset.requested_at + _TTL
    assert found.requested_at.microsecond == 0


async def test_issuing_swaps_the_address_for_the_account_in_the_row_and_in_the_aggregate(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The Core `UPDATE` writes both target columns (the mapper cannot see `_target` change), so the
    address is dropped and the account set in one statement, and both CHECKs hold."""
    repo = SqlAlchemyPasswordResetRepository(session)
    owner = await persist_user(session, clock)
    issued = await _issued_for(session, repo, clock, owner.user_id, _hash("1"))

    stored = await _row(session, issued.id)
    assert stored.email is None
    assert stored.user_id == owner.user_id.value
    assert stored.token_hash == _hash("1").value
    assert stored.issued_at == issued.issued_at
    session.expunge_all()
    found = await repo.get(issued.id)
    assert found is not None
    assert found.target == IssuedReset(owner.user_id)
    assert found.user_id == owner.user_id
    assert found.token_hash == _hash("1")


async def test_getting_an_unknown_id_is_none(session: AsyncSession) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)

    assert await repo.get(PasswordResetId(uuid4())) is None


async def test_find_by_token_hash_finds_the_issued_reset_without_taking_a_lock(
    session: AsyncSession, connection: AsyncConnection, clock: FixedClock
) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)
    owner = await persist_user(session, clock)
    issued = await _issued_for(session, repo, clock, owner.user_id, _hash("2"))
    session.expunge_all()

    with _captured_statements(connection) as statements:
        found = await repo.find_by_token_hash(_hash("2"))
        missing = await repo.find_by_token_hash(_hash("3"))

    assert found is not None
    assert found.id == issued.id
    assert found.target == IssuedReset(owner.user_id)
    assert missing is None
    selects = [s for s in statements if "identity_password_reset" in s]
    assert selects
    assert all("FOR UPDATE" not in s.upper() for s in selects)


async def test_lock_by_token_hash_takes_for_update_and_hands_back_an_issued_target(
    session: AsyncSession, connection: AsyncConnection, clock: FixedClock
) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)
    owner = await persist_user(session, clock)
    issued = await _issued_for(session, repo, clock, owner.user_id, _hash("4"))
    session.expunge_all()

    with _captured_statements(connection) as statements:
        found = await repo.lock_by_token_hash(_hash("4"))

    assert found is not None
    assert found.id == issued.id
    assert found.user_id == owner.user_id
    (select_statement,) = [s for s in statements if "identity_password_reset" in s]
    assert select_statement.rstrip().upper().endswith("FOR UPDATE")


async def test_lock_by_token_hash_rebuilds_the_target_of_an_instance_the_row_outgrew(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`populate_existing` fires the mapping's `refresh` hook, which rebuilds `_target`. The session
    holds an *addressed* instance; the row becomes *issued* underneath it; the locked read must hand
    back the issued target, not the one first read. (Without `populate_existing` the identity-map hit
    keeps the stale instance.)"""
    repo = SqlAlchemyPasswordResetRepository(session)
    owner = await persist_user(session, clock)
    reset = _request(repo, clock, _address())
    await repo.add(reset)
    session.expunge_all()
    stale = await repo.get(reset.id)
    assert stale is not None
    assert isinstance(stale.target, AddressedReset)
    await session.execute(
        text(
            "UPDATE identity_password_reset SET email = NULL, user_id = :u, token_hash = :h, "
            "issued_at = :t WHERE id = :i"
        ),
        {"u": owner.user_id.value, "h": _hash("5").value, "t": clock.now(), "i": reset.id.value},
    )

    locked = await repo.lock_by_token_hash(_hash("5"))

    assert locked is not None
    assert locked.target == IssuedReset(owner.user_id)
    assert locked.user_id == owner.user_id


async def test_lock_by_token_hash_with_an_unknown_hash_is_none(session: AsyncSession) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)

    assert await repo.lock_by_token_hash(_hash("e")) is None


# ------------------------------------------------------------------------------------ the supersede


async def test_issuing_deletes_only_that_users_other_resets(
    session: AsyncSession, clock: FixedClock
) -> None:
    """One live reset link per account. Spared: another user's resets, and an *addressed* reset for
    the same address (it is matched to an account only by its own delivery)."""
    repo = SqlAlchemyPasswordResetRepository(session)
    mine = await persist_user(session, clock)
    theirs = await persist_user(session, clock)
    older_1 = await _issued_for(session, repo, clock, mine.user_id, _hash("a"))
    older_2 = await _issued_for(session, repo, clock, mine.user_id, _hash("b"))
    others = await _issued_for(session, repo, clock, theirs.user_id, _hash("c"))
    addressed = _request(repo, clock, _address())
    await repo.add(addressed)

    newest = await _issued_for(session, repo, clock, mine.user_id, _hash("d"))

    assert await _ids_of(session, mine.user_id) == {newest.id.value}
    assert await _row(session, older_1.id) is None
    assert await _row(session, older_2.id) is None
    assert await _ids_of(session, theirs.user_id) == {others.id.value}
    assert await _row(session, addressed.id) is not None


async def test_a_delivery_that_loses_the_issue_supersedes_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The `UPDATE … WHERE token_hash IS NULL` runs first and its refusal comes *before* the
    `DELETE`: a loser must not delete the resets of an account it never issued one to."""
    repo = SqlAlchemyPasswordResetRepository(session)
    owner = await persist_user(session, clock)
    pending = _request(repo, clock, _address())
    await repo.add(pending)
    session.expunge_all()
    winner = await repo.get(pending.id)
    session.expunge_all()
    loser = await repo.get(pending.id)
    assert winner is not None
    assert loser is not None
    winner.issue(owner.user_id, _hash("1"), clock.now())
    loser.issue(owner.user_id, _hash("2"), clock.now())
    await repo.save_issued(winner)
    bystander = await _issued_for_raw(session, clock, owner.user_id, _hash("9"))

    with pytest.raises(PasswordResetAlreadyIssued):
        await repo.save_issued(loser)

    assert bystander.value in await _ids_of(session, owner.user_id)
    stored = await _row(session, pending.id)
    assert stored.token_hash == _hash("1").value


async def _issued_for_raw(
    session: AsyncSession, clock: FixedClock, user_id: UserId, token_hash: TokenHash
) -> PasswordResetId:
    """An issued reset inserted by hand (a stand-in for another delivery's committed result)."""
    reset_id = PasswordResetId(uuid4())
    await session.execute(
        text(
            "INSERT INTO identity_password_reset (id, user_id, token_hash, requested_at, "
            "expires_at, issued_at) VALUES (:i, :u, :h, :t, :x, :t)"
        ),
        {
            "i": reset_id.value,
            "u": user_id.value,
            "h": token_hash.value,
            "t": clock.now(),
            "x": clock.now() + _TTL,
        },
    )
    return reset_id


async def test_save_issued_on_a_reset_that_was_removed_is_refused(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)
    owner = await persist_user(session, clock)
    reset = _request(repo, clock, _address())
    await repo.add(reset)
    session.expunge_all()
    loaded = await repo.get(reset.id)
    assert loaded is not None
    loaded.issue(owner.user_id, _hash("1"), clock.now())
    await repo.remove(reset.id)

    with pytest.raises(PasswordResetAlreadyIssued):
        await repo.save_issued(loaded)


async def test_superseded_instances_leave_the_identity_map(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)
    owner = await persist_user(session, clock)
    older = await _issued_for(session, repo, clock, owner.user_id, _hash("a"))
    older_loaded = await repo.get(older.id)
    assert older_loaded is not None
    assert older_loaded in session

    await _issued_for(session, repo, clock, owner.user_id, _hash("b"))

    assert older_loaded not in session


# -------------------------------------------------------------------------------- remove variants


async def test_remove_deletes_one_reset_and_is_idempotent(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)
    reset = _request(repo, clock, _address())
    other = _request(repo, clock, _address())
    await repo.add(reset)
    await repo.add(other)

    await repo.remove(reset.id)
    await repo.remove(reset.id)
    await repo.remove(PasswordResetId(uuid4()))

    assert await _row(session, reset.id) is None
    assert await _row(session, other.id) is not None


async def test_remove_all_for_user_deletes_only_that_users_issued_resets_and_counts_them(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)
    mine = await persist_user(session, clock)
    theirs = await persist_user(session, clock)
    await _issued_for_raw(session, clock, mine.user_id, _hash("1"))
    await _issued_for_raw(session, clock, mine.user_id, _hash("2"))
    survivor = await _issued_for_raw(session, clock, theirs.user_id, _hash("3"))
    addressed = _request(repo, clock, _address())
    await repo.add(addressed)

    removed = await repo.remove_all_for_user(mine.user_id)

    assert removed == 2
    assert await _ids_of(session, mine.user_id) == set()
    assert await _ids_of(session, theirs.user_id) == {survivor.value}
    assert await _row(session, addressed.id) is not None, "an addressed reset has no user_id"


async def test_remove_all_for_a_user_with_none_is_zero(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)
    owner = await persist_user(session, clock)

    assert await repo.remove_all_for_user(owner.user_id) == 0


async def test_remove_all_for_user_expunges_the_instances_it_deleted(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPasswordResetRepository(session)
    owner = await persist_user(session, clock)
    issued = await _issued_for(session, repo, clock, owner.user_id, _hash("1"))
    loaded = await repo.get(issued.id)
    assert loaded is not None
    assert loaded in session

    await repo.remove_all_for_user(owner.user_id)

    assert loaded not in session


# ------------------------------------------------------------------------------- real connections


@asynccontextmanager
async def _committed_reset(
    engine: AsyncEngine, clock: FixedClock, settings: Any
) -> AsyncIterator[tuple[PasswordResetId, UserId]]:
    """A committed user and a committed addressed reset; both deleted afterwards."""
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed:
        repo = SqlAlchemyPasswordResetRepository(seed)
        reset = _request(repo, clock, _address())
        await repo.add(reset)
        await seed.commit()
    try:
        yield reset.id, user_id
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_password_reset WHERE id = :i"), {"i": reset.id.value}
            )
            await conn.execute(
                text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id.value}
            )


async def test_two_deliveries_racing_to_issue_one_reset_mail_one_link(
    engine: AsyncEngine, clock: FixedClock, settings: Any
) -> None:
    """Two real connections, as for the pending registration: A's guarded `UPDATE` holds the row,
    B's is observed waiting, A commits, B is refused `PasswordResetAlreadyIssued` and the row holds
    A's hash."""
    async with _committed_reset(engine, clock, settings) as (reset_id, user_id):
        async with pinned_session(engine) as a, pinned_session(engine) as b:
            repo_a = SqlAlchemyPasswordResetRepository(a)
            repo_b = SqlAlchemyPasswordResetRepository(b)
            row_a = await repo_a.get(reset_id)
            row_b = await repo_b.get(reset_id)
            assert row_a is not None
            assert row_b is not None
            row_a.issue(user_id, _hash("1"), clock.now())
            row_b.issue(user_id, _hash("2"), clock.now())
            await repo_a.save_issued(row_a)

            loser = asyncio.create_task(repo_b.save_issued(row_b))
            await wait_for_lock_waiter(engine, "update identity_password_reset")
            assert not loser.done()
            await a.commit()
            with pytest.raises(PasswordResetAlreadyIssued):
                await asyncio.wait_for(loser, _STEP_TIMEOUT)
            await b.rollback()

        async with engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT token_hash, user_id FROM identity_password_reset WHERE id = :i"),
                    {"i": reset_id.value},
                )
            ).one()
        assert (stored[0], stored[1]) == (_hash("1").value, user_id.value)
