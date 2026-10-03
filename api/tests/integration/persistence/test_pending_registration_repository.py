"""`SqlAlchemyPendingRegistrationRepository` against real PostgreSQL (slice 2.5, T23, test-after;
AC-17 and the issued-once / double-confirm rules of plan §0.3, §0.4, §0.8).

Three styles, each for the thing it can prove:

- **Rolled-back tests on the `session` fixture** for statement shape and single-session behaviour.
  Statement capture is `before_cursor_execute` on the fixture's connection: the text that reached
  the driver, not the SQLAlchemy construct that produced it.
- **Two real, pinned connections** (`claim_race_support.pinned_session`) for the races. Each race is
  *staged at the moment the spec names*: connection A holds the row or the unique-index entry
  uncommitted, B's statement is observed **waiting on the lock** in `pg_stat_activity`
  (`wait_for_lock_waiter`), and only then does A commit. A `sleep` cannot tell "blocked" from "not
  yet scheduled" (2.3's H-33). `lock_timeout` is on every connection, so a regression that makes a
  loser wait for ever fails in seconds and names a lock.
- **Committed seeds, scoped to ids and addresses the test created**, each deleted in a `finally`.
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

# Imported for its side effect (`map_imperatively`): `claim_race_support` imports repositories that
# read mapped attributes at import time, and run alone this file would reach them before any mapping.
import tailorcraft.infrastructure.persistence.retention.account_data  # noqa: F401
from tailorcraft.domain.identity.errors import PendingRegistrationAlreadyIssued
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    PendingRegistrationId,
    TokenHash,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.identity.pending_registration import (
    SqlAlchemyPendingRegistrationRepository,
)
from tests.integration.claim_race_support import (
    assert_test_database,
    pinned_session,
    wait_for_lock_waiter,
)

_HASH_1 = TokenHash("1" * 64)
_HASH_2 = TokenHash("2" * 64)
_TTL = timedelta(hours=24)
_STEP_TIMEOUT = 15.0


def _password_hash(label: str = "a") -> PasswordHash:
    return PasswordHash(f"$argon2id$v=19$m=65536,t=3,p=4$c2FsdA${label}aGFzaA")


def _address() -> EmailAddress:
    return EmailAddress.parse(f"pending-{uuid4().hex}@example.com")


def _request(
    repo: SqlAlchemyPendingRegistrationRepository,
    clock: FixedClock,
    email: EmailAddress,
    *,
    label: str = "a",
    at_offset: timedelta = timedelta(0),
) -> PendingRegistration:
    return PendingRegistration.request(
        id=repo.next_identity(),
        email=email,
        password_hash=_password_hash(label),
        at=clock.now() + at_offset,
        ttl=_TTL,
    )


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


async def _rows_for(session: AsyncSession, email: EmailAddress) -> list[Any]:
    return list(
        (
            await session.execute(
                text(
                    "SELECT id, password_hash, requested_at, expires_at, token_hash, issued_at "
                    "FROM identity_pending_registration WHERE email = :e"
                ),
                {"e": email.value},
            )
        ).all()
    )


# --------------------------------------------------------------------------- single-session tests


async def test_a_put_pending_registration_round_trips_with_whole_second_instants(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)
    pending = _request(repo, clock, _address())

    await repo.put(pending)
    session.expunge_all()
    found = await repo.get(pending.id)

    assert found is not None
    assert found.id == pending.id
    assert found.email == pending.email
    assert found.password_hash == pending.password_hash
    assert found.requested_at == pending.requested_at
    assert found.expires_at == pending.expires_at == pending.requested_at + _TTL
    assert found.token_hash is None
    assert found.issued_at is None
    assert found.requested_at.microsecond == 0


async def test_getting_an_unknown_id_is_none(session: AsyncSession) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)

    assert await repo.get(PendingRegistrationId(uuid4())) is None


async def test_a_second_put_for_the_address_replaces_the_row_and_clears_the_token(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The supersede: new id, new hash, new instants, token columns nulled — on an *issued* row."""
    repo = SqlAlchemyPendingRegistrationRepository(session)
    email = _address()
    first = _request(repo, clock, email, label="a")
    await repo.put(first)
    session.expunge_all()
    loaded = await repo.get(first.id)
    assert loaded is not None
    loaded.issue(_HASH_1, clock.now() + timedelta(minutes=1))
    await repo.save_issued(loaded)
    second = _request(repo, clock, email, label="b", at_offset=timedelta(minutes=5))

    await repo.put(second)

    session.expunge_all()
    ((row_id, password_hash, requested_at, expires_at, token_hash, issued_at),) = await _rows_for(
        session, email
    )
    assert row_id == second.id.value
    assert password_hash == second.password_hash.value
    assert requested_at == second.requested_at
    assert expires_at == second.expires_at
    assert token_hash is None
    assert issued_at is None
    assert await repo.get(first.id) is None, "a delivery queued for the old id must find nothing"


async def test_puts_for_different_addresses_leave_two_rows(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)
    a, b = _address(), _address()

    await repo.put(_request(repo, clock, a))
    await repo.put(_request(repo, clock, b))

    assert len(await _rows_for(session, a)) == 1
    assert len(await _rows_for(session, b)) == 1


async def test_put_is_one_upsert_statement_with_no_read_first(
    session: AsyncSession, connection: AsyncConnection, clock: FixedClock
) -> None:
    """AC-17: one `INSERT … ON CONFLICT (email) DO UPDATE`, and no `SELECT` on the table before it
    (a read first would be a timing oracle and a race)."""
    repo = SqlAlchemyPendingRegistrationRepository(session)
    email = _address()
    await repo.put(_request(repo, clock, email))  # the address exists: the conflict path

    with _captured_statements(connection) as statements:
        await repo.put(_request(repo, clock, email, label="b"))

    touching = [s for s in statements if "identity_pending_registration" in s]
    assert len(touching) == 1, touching
    assert touching[0].lstrip().upper().startswith("INSERT INTO IDENTITY_PENDING_REGISTRATION")
    assert "ON CONFLICT (email) DO UPDATE" in touching[0]


async def test_save_issued_stores_the_hash_and_the_instant(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)
    pending = _request(repo, clock, _address())
    await repo.put(pending)
    session.expunge_all()
    loaded = await repo.get(pending.id)
    assert loaded is not None
    issued_at = clock.now() + timedelta(minutes=2)
    loaded.issue(_HASH_1, issued_at)

    await repo.save_issued(loaded)

    session.expunge_all()
    reloaded = await repo.get(pending.id)
    assert reloaded is not None
    assert reloaded.token_hash == _HASH_1
    assert reloaded.issued_at == issued_at


async def test_a_second_delivery_that_loaded_the_row_unissued_is_refused(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The issued-once guard is in the `UPDATE`'s `WHERE`, not in a read before it: two instances
    that both saw the row unissued both pass the aggregate's own check, and the statement refuses
    the second."""
    repo = SqlAlchemyPendingRegistrationRepository(session)
    pending = _request(repo, clock, _address())
    await repo.put(pending)
    session.expunge_all()
    winner = await repo.get(pending.id)
    session.expunge_all()
    loser = await repo.get(pending.id)
    assert winner is not None
    assert loser is not None
    assert winner is not loser
    winner.issue(_HASH_1, clock.now())
    loser.issue(_HASH_2, clock.now())

    await repo.save_issued(winner)
    with pytest.raises(PendingRegistrationAlreadyIssued):
        await repo.save_issued(loser)

    session.expunge_all()
    row = await repo.get(pending.id)
    assert row is not None
    assert row.token_hash == _HASH_1, "the loser must not overwrite the winner's token"


async def test_save_issued_on_a_row_that_was_removed_or_superseded_is_refused(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)
    email = _address()
    first = _request(repo, clock, email)
    await repo.put(first)
    session.expunge_all()
    stale = await repo.get(first.id)
    assert stale is not None
    stale.issue(_HASH_1, clock.now())
    await repo.put(_request(repo, clock, email, label="b"))  # supersedes: the old id is gone

    with pytest.raises(PendingRegistrationAlreadyIssued):
        await repo.save_issued(stale)

    assert await repo.get(first.id) is None


async def test_lock_by_token_hash_finds_the_issued_row_under_for_update(
    session: AsyncSession, connection: AsyncConnection, clock: FixedClock
) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)
    pending = _request(repo, clock, _address())
    await repo.put(pending)
    session.expunge_all()
    loaded = await repo.get(pending.id)
    assert loaded is not None
    loaded.issue(_HASH_1, clock.now())
    await repo.save_issued(loaded)
    session.expunge_all()

    with _captured_statements(connection) as statements:
        found = await repo.lock_by_token_hash(_HASH_1)

    assert found is not None
    assert found.id == pending.id
    (select_statement,) = [s for s in statements if "identity_pending_registration" in s]
    assert select_statement.rstrip().upper().endswith("FOR UPDATE")
    assert "token_hash" in select_statement.split("WHERE", 1)[1]


async def test_lock_by_token_hash_with_an_unknown_hash_is_none(session: AsyncSession) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)

    assert await repo.lock_by_token_hash(TokenHash("e" * 64)) is None


async def test_remove_deletes_the_row_and_is_idempotent(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)
    email = _address()
    pending = _request(repo, clock, email)
    await repo.put(pending)

    await repo.remove(pending.id)
    await repo.remove(pending.id)  # nothing to delete is success
    await repo.remove(PendingRegistrationId(uuid4()))

    assert await _rows_for(session, email) == []


async def test_remove_drops_a_loaded_instance_from_the_identity_map(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)
    pending = _request(repo, clock, _address())
    await repo.put(pending)
    session.expunge_all()
    loaded = await repo.get(pending.id)
    assert loaded is not None
    assert loaded in session

    await repo.remove(pending.id)

    assert loaded not in session


async def test_next_identity_is_a_fresh_pending_registration_id_each_time(
    session: AsyncSession,
) -> None:
    repo = SqlAlchemyPendingRegistrationRepository(session)

    ids = {repo.next_identity() for _ in range(20)}

    assert len(ids) == 20


# ------------------------------------------------------------------------------ real connections


@asynccontextmanager
async def _committed_pending(
    engine: AsyncEngine, clock: FixedClock, *, issue_with: TokenHash | None = None
) -> AsyncIterator[PendingRegistration]:
    """A committed row, deleted afterwards by address."""
    assert_test_database_url(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed:
        repo = SqlAlchemyPendingRegistrationRepository(seed)
        pending = _request(repo, clock, _address())
        await repo.put(pending)
        await seed.commit()
        if issue_with is not None:
            seed.expunge_all()
            loaded = await repo.get(pending.id)
            assert loaded is not None
            loaded.issue(issue_with, clock.now())
            await repo.save_issued(loaded)
            await seed.commit()
    try:
        yield pending
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_pending_registration WHERE email = :e"),
                {"e": pending.email.value},
            )


def assert_test_database_url(engine: AsyncEngine) -> None:
    assert engine.url.database is not None
    assert engine.url.database.endswith("_test"), engine.url


async def test_two_concurrent_puts_for_one_address_leave_one_row_and_raise_nothing(
    engine: AsyncEngine, clock: FixedClock, settings: Any
) -> None:
    """AC-17, staged: A's upsert holds the unique-index entry uncommitted; B's upsert is observed
    blocked on it; A commits; B then takes the `ON CONFLICT … DO UPDATE` path. Exactly one row, and
    the later statement wins whole."""
    assert_test_database(settings)
    email = _address()
    try:
        async with pinned_session(engine) as a, pinned_session(engine) as b:
            repo_a = SqlAlchemyPendingRegistrationRepository(a)
            repo_b = SqlAlchemyPendingRegistrationRepository(b)
            first = _request(repo_a, clock, email, label="a")
            second = _request(repo_b, clock, email, label="b")
            await repo_a.put(first)  # uncommitted: the index entry is held

            blocked = asyncio.create_task(repo_b.put(second))
            await wait_for_lock_waiter(engine, "insert into identity_pending_registration")
            assert not blocked.done(), "B must be waiting on A's uncommitted insert"
            await a.commit()
            await asyncio.wait_for(blocked, _STEP_TIMEOUT)  # raises nothing
            await b.commit()

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, password_hash FROM identity_pending_registration "
                        "WHERE email = :e"
                    ),
                    {"e": email.value},
                )
            ).all()
        assert [(r[0], r[1]) for r in rows] == [(second.id.value, second.password_hash.value)]
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_pending_registration WHERE email = :e"),
                {"e": email.value},
            )


async def test_two_deliveries_racing_to_issue_one_row_send_one_mail(
    engine: AsyncEngine, clock: FixedClock, settings: Any
) -> None:
    """AC-39's persistence half, on two real connections: both loaded the row unissued; A's guarded
    `UPDATE` is uncommitted; B's is observed waiting; A commits; B re-evaluates `token_hash IS NULL`
    on the committed row, matches nothing and is refused. Without the predicate in the statement B
    would overwrite A's token and both would mail a link."""
    assert_test_database(settings)
    async with (
        _committed_pending(engine, clock) as pending,
        pinned_session(engine) as a,
        pinned_session(engine) as b,
    ):
        repo_a = SqlAlchemyPendingRegistrationRepository(a)
        repo_b = SqlAlchemyPendingRegistrationRepository(b)
        row_a = await repo_a.get(pending.id)
        row_b = await repo_b.get(pending.id)
        assert row_a is not None
        assert row_b is not None
        row_a.issue(_HASH_1, clock.now())
        row_b.issue(_HASH_2, clock.now())
        await repo_a.save_issued(row_a)  # uncommitted: holds the row lock

        loser = asyncio.create_task(repo_b.save_issued(row_b))
        await wait_for_lock_waiter(engine, "update identity_pending_registration")
        assert not loser.done()
        await a.commit()
        with pytest.raises(PendingRegistrationAlreadyIssued):
            await asyncio.wait_for(loser, _STEP_TIMEOUT)
        await b.rollback()

        async with engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT token_hash FROM identity_pending_registration WHERE id = :i"),
                    {"i": pending.id.value},
                )
            ).scalar_one()
        assert stored == _HASH_1.value


async def test_a_second_confirm_of_one_link_waits_and_then_finds_nothing(
    engine: AsyncEngine, clock: FixedClock, settings: Any
) -> None:
    """Double confirm: A holds the row `FOR UPDATE`, B's `lock_by_token_hash` is observed waiting,
    A removes the row and commits, and B (READ COMMITTED re-checks the locked row) gets `None` —
    so only one `User` can be built from one link."""
    assert_test_database(settings)
    async with (
        _committed_pending(engine, clock, issue_with=_HASH_1),
        pinned_session(engine) as a,
        pinned_session(engine) as b,
    ):
        repo_a = SqlAlchemyPendingRegistrationRepository(a)
        repo_b = SqlAlchemyPendingRegistrationRepository(b)
        held = await repo_a.lock_by_token_hash(_HASH_1)
        assert held is not None

        second = asyncio.create_task(repo_b.lock_by_token_hash(_HASH_1))
        await wait_for_lock_waiter(engine, "identity_pending_registration", "for update")
        assert not second.done()
        await repo_a.remove(held.id)
        await a.commit()

        assert await asyncio.wait_for(second, _STEP_TIMEOUT) is None
        await b.rollback()


async def test_lock_by_token_hash_overwrites_an_instance_this_session_read_earlier(
    engine: AsyncEngine, clock: FixedClock, settings: Any
) -> None:
    """`populate_existing`: a session that read the row, then waited for a lock while another
    transaction changed it, must act on the row **as it is now**. Here another connection moves
    `expires_at` between this session's `get` and its `lock_by_token_hash`."""
    assert_test_database(settings)
    async with (
        _committed_pending(engine, clock, issue_with=_HASH_1) as pending,
        pinned_session(engine) as mine,
    ):
        repo = SqlAlchemyPendingRegistrationRepository(mine)
        before = await repo.get(pending.id)
        assert before is not None
        old_expiry = before.expires_at
        new_expiry = old_expiry + timedelta(hours=1)
        async with engine.begin() as other:
            await other.execute(
                text("UPDATE identity_pending_registration SET expires_at = :x WHERE id = :i"),
                {"x": new_expiry, "i": pending.id.value},
            )

        locked = await repo.lock_by_token_hash(_HASH_1)

        assert locked is not None
        assert locked.expires_at == new_expiry
        await mine.rollback()
