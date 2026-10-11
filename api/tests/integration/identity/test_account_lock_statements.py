"""AC-12 (slice 4.2) by statement capture: the SQL the account use cases really emit.

A keyword argument's name is not documentation (CLAUDE.md), so each assertion reads the compiled
clause: `DisableAccount` / `EnableAccount` read the user `FOR UPDATE` (not `FOR NO KEY UPDATE`),
`LogIn`'s credential re-check is `FOR SHARE` (and its other reads take no lock), `ListUsers` and
`GetAccountOverview` take none, and `DisableAccount` locks the user **before** it deletes the logins
(ADR-0028's order).

Mutations, observed 2026-10-11 and restored byte-exact (`git diff --stat -- src` empty), are in the
commit body that added this file.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.disable_account import DisableAccount
from tailorcraft.application.identity.enable_account import EnableAccount
from tailorcraft.application.identity.list_users import ListUsers
from tailorcraft.application.identity.log_in import LogIn
from tailorcraft.application.retention.get_account_overview import GetAccountOverview
from tailorcraft.domain.identity.value_objects import PasswordVerdict
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.queries.user_directory import (
    SqlAlchemyUserDirectoryQuery,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tests.integration.fakes import (
    FakeAccessTokenPort,
    RecordingEventPublisher,
    RecordingFailedLoginObserver,
    RecordingPasswordHasher,
)
from tests.integration.identity._account_support import (
    PHC,
    SpyLogins,
    SpyUsers,
    seed_user,
    token_hash,
)

_FOR = re.compile(r"\bFOR\s+(NO KEY UPDATE|KEY SHARE|UPDATE|SHARE)\b", re.IGNORECASE)


@contextmanager
def _captured(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = session.get_bind()

    def capture(conn: Connection, cursor: object, statement: str, *_: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _is_user_select(statement: str) -> bool:
    return "identity_user" in statement and statement.lstrip().upper().startswith("SELECT")


def _user_locks(statements: list[str]) -> list[str]:
    return [m.group(1).upper() for s in statements if _is_user_select(s) if (m := _FOR.search(s))]


async def test_disable_account_reads_the_user_for_update_before_deleting_the_logins(
    session: AsyncSession, clock: FixedClock
) -> None:
    target = await seed_user(session, clock, logins=2)
    log: list[str] = []
    use_case = DisableAccount(
        SpyUsers(session, log), SpyLogins(session, log), clock, RecordingEventPublisher()
    )

    with _captured(session) as statements:
        await use_case(target)

    assert _user_locks(statements) == ["UPDATE"], statements
    lock_at = next(i for i, s in enumerate(statements) if _is_user_select(s) and _FOR.search(s))
    delete_at = next(
        i
        for i, s in enumerate(statements)
        if s.lstrip().upper().startswith("DELETE FROM IDENTITY_LOGIN")
    )
    assert lock_at < delete_at, "the user row must be locked before its logins are deleted"


async def test_enable_account_reads_the_user_for_update(
    session: AsyncSession, clock: FixedClock
) -> None:
    target = await seed_user(session, clock, disabled=True)
    use_case = EnableAccount(SpyUsers(session, []), clock, RecordingEventPublisher())
    clock.advance(5)

    with _captured(session) as statements:
        await use_case(target)

    assert _user_locks(statements) == ["UPDATE"], statements


async def test_the_logins_recheck_is_for_share_and_no_other_user_read_locks(
    session: AsyncSession, clock: FixedClock
) -> None:
    address = f"{uuid4().hex[:12]}@example.com"
    await seed_user(session, clock, email=address)
    use_case = LogIn(
        SpyUsers(session, []),
        SpyLogins(session, []),
        RecordingPasswordHasher(verify_result=PasswordVerdict.MATCH, hash_result=PHC),
        FakeAccessTokenPort(),
        clock,
        RecordingEventPublisher(),
        RecordingFailedLoginObserver(),
        timedelta(days=30),
    )

    with _captured(session) as statements:
        await use_case(address, "the correct password", token_hash("r"))

    assert _user_locks(statements) == ["SHARE"], statements
    assert sum(1 for s in statements if _is_user_select(s)) >= 3, (
        "premise: the lookup, the re-check and the disabled read all ran"
    )


async def test_list_users_takes_no_lock(session: AsyncSession, clock: FixedClock) -> None:
    await seed_user(session, clock)

    with _captured(session) as statements:
        await ListUsers(SqlAlchemyUserDirectoryQuery(session))(None, None, 5)

    assert any(_is_user_select(s) for s in statements), "premise: the query ran"
    assert [s for s in statements if _FOR.search(s)] == []


async def test_get_account_overview_takes_no_lock(session: AsyncSession, clock: FixedClock) -> None:
    target = await seed_user(session, clock, logins=1)
    use_case = GetAccountOverview(SqlAlchemyUserRepository(session), SqlAlchemyAccountData(session))

    with _captured(session) as statements:
        await use_case(target)

    assert any(_is_user_select(s) for s in statements), "premise: the use case ran"
    assert [s for s in statements if _FOR.search(s)] == []
