"""Erasure by address (slice 2.5, T23, test-after; AC-19): `SqlAlchemyAccountData.delete_account`
also removes the account's **addressed** password resets and any **pending registration** for its
address, and nothing belonging to another address.

Why a test of its own: an *issued* reset leaves with the account by `ON DELETE CASCADE`, but an
addressed reset and a pending registration name only the address — no foreign key reaches them — so an
erased account would otherwise leave its address (and, in a pending row, a password hash) in the
database for up to a day. The rows the test did not expect to go are asserted to stay: a deletion that
is too wide is the worse failure.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tests.integration.persistence.owner_rows import persist_user

_PHC = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"


async def _email_of(session: AsyncSession, user_id: UserId) -> str:
    return str(
        (
            await session.execute(
                text("SELECT email FROM identity_user WHERE id = :i"), {"i": user_id.value}
            )
        ).scalar_one()
    )


async def _pending(session: AsyncSession, clock: FixedClock, email: str) -> Any:
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_pending_registration (id, email, password_hash, requested_at, "
            "expires_at) VALUES (:i, :e, :p, :t, :x)"
        ),
        {
            "i": row_id,
            "e": email,
            "p": _PHC,
            "t": clock.now(),
            "x": clock.now() + timedelta(hours=24),
        },
    )
    return row_id


async def _addressed_reset(session: AsyncSession, clock: FixedClock, email: str) -> Any:
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_password_reset (id, email, requested_at, expires_at) "
            "VALUES (:i, :e, :t, :x)"
        ),
        {"i": row_id, "e": email, "t": clock.now(), "x": clock.now() + timedelta(hours=1)},
    )
    return row_id


async def _issued_reset(
    session: AsyncSession, clock: FixedClock, user_id: UserId, token_char: str
) -> Any:
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_password_reset (id, user_id, token_hash, requested_at, "
            "expires_at, issued_at) VALUES (:i, :u, :h, :t, :x, :t)"
        ),
        {
            "i": row_id,
            "u": user_id.value,
            "h": token_char * 64,
            "t": clock.now(),
            "x": clock.now() + timedelta(hours=1),
        },
    )
    return row_id


async def _present(session: AsyncSession, table: str, row_id: Any) -> bool:
    assert table in {"identity_pending_registration", "identity_password_reset"}
    count = (
        await session.execute(text(f"SELECT count(*) FROM {table} WHERE id = :i"), {"i": row_id})  # noqa: S608 -- allow-listed above
    ).scalar_one()
    return bool(count)


async def test_deleting_an_account_removes_its_pending_registration_and_addressed_resets(
    session: AsyncSession, clock: FixedClock
) -> None:
    data = SqlAlchemyAccountData(session)
    owner = await persist_user(session, clock)
    email = await _email_of(session, owner.user_id)
    pending = await _pending(session, clock, email)
    addressed_1 = await _addressed_reset(session, clock, email)
    addressed_2 = await _addressed_reset(session, clock, email)
    issued = await _issued_reset(session, clock, owner.user_id, "a")

    await data.files_of_account(owner.user_id)
    deleted = await data.delete_account(owner.user_id)

    assert deleted is True
    assert not await _present(session, "identity_pending_registration", pending)
    assert not await _present(session, "identity_password_reset", addressed_1)
    assert not await _present(session, "identity_password_reset", addressed_2)
    assert not await _present(session, "identity_password_reset", issued), "the cascade's half"


async def test_deleting_an_account_leaves_other_addresses_one_time_rows_alone(
    session: AsyncSession, clock: FixedClock
) -> None:
    data = SqlAlchemyAccountData(session)
    owner = await persist_user(session, clock)
    bystander = await persist_user(session, clock)
    other_email = await _email_of(session, bystander.user_id)
    unrelated = f"stranger-{uuid4().hex}@example.com"
    kept = [
        ("identity_pending_registration", await _pending(session, clock, other_email)),
        ("identity_pending_registration", await _pending(session, clock, unrelated)),
        ("identity_password_reset", await _addressed_reset(session, clock, other_email)),
        ("identity_password_reset", await _addressed_reset(session, clock, unrelated)),
        ("identity_password_reset", await _issued_reset(session, clock, bystander.user_id, "b")),
    ]

    await data.files_of_account(owner.user_id)
    assert await data.delete_account(owner.user_id) is True

    for table, row_id in kept:
        assert await _present(session, table, row_id), f"{table} row of another address was deleted"


async def test_an_account_with_no_one_time_rows_is_still_deleted(
    session: AsyncSession, clock: FixedClock
) -> None:
    data = SqlAlchemyAccountData(session)
    owner = await persist_user(session, clock)

    await data.files_of_account(owner.user_id)

    assert await data.delete_account(owner.user_id) is True
    assert (
        await session.execute(
            text("SELECT count(*) FROM identity_user WHERE id = :i"), {"i": owner.user_id.value}
        )
    ).scalar_one() == 0


async def test_deleting_an_account_that_does_not_exist_deletes_nothing_and_says_so(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The address read comes from the locked user row: no row, no address, so no by-address delete
    could even be attempted, and the answer is `False`. A stranger's pending row for some address
    survives."""
    data = SqlAlchemyAccountData(session)
    bystander = f"stranger-{uuid4().hex}@example.com"
    kept = await _pending(session, clock, bystander)

    assert await data.delete_account(UserId(uuid4())) is False

    assert await _present(session, "identity_pending_registration", kept)
