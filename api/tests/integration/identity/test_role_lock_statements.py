"""AC-8 (slice 4.1) by statement capture: the SQL the two role use cases really emit.

`ChangeUserRole` must read the user row `FOR UPDATE` -- not `FOR SHARE`, `FOR NO KEY UPDATE` or
`FOR KEY SHARE` (CLAUDE.md: a keyword argument's name is not documentation; read the SQL).
`AuthorizeAdministrator` must emit no `FOR` clause at all (a plain read, never a lock).
The spy-based tests in this folder only see which repository method was called; this sees the lock.

**Mutation 3, observed 2026-10-10**: `get_for_update` -> `get` in `change_user_role.py` (restored
byte-exact, `git diff` over src empty): the two new
tests (dry-run, real) went red, `assert [] == ['UPDATE']` (the captured SELECT ends at `WHERE identity_user.id = $1`); the
spy test also went red, `assert 'get' == 'get_for_update'` (4 red in all).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.authorize_administrator import AuthorizeAdministrator
from tailorcraft.application.identity.change_user_role import ChangeUserRole
from tailorcraft.domain.identity.value_objects import Role
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import RecordingEventPublisher
from tests.integration.identity._role_support import SpyUsers, seed_user

_FOR = re.compile(r"\bFOR\s+(NO KEY UPDATE|KEY SHARE|UPDATE|SHARE)\b", re.IGNORECASE)


@contextmanager
def _user_selects(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = session.get_bind()

    def capture(conn: Connection, cursor: object, statement: str, *_: object) -> None:
        if "identity_user" in statement and statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "real"])
async def test_change_user_role_reads_the_user_row_for_update(
    session: AsyncSession, clock: FixedClock, dry_run: bool
) -> None:
    user_id = await seed_user(session, clock, Role.USER)
    use_case = ChangeUserRole(SpyUsers(session), clock, RecordingEventPublisher())

    with _user_selects(session) as selects:
        await use_case(user_id, Role.ADMIN, dry_run=dry_run)

    assert selects, "no SELECT on identity_user was captured"
    locks = [m.group(1).upper() for s in selects if (m := _FOR.search(s))]
    assert locks == ["UPDATE"], selects


async def test_authorize_administrator_emits_no_for_clause(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, Role.ADMIN)

    with _user_selects(session) as selects:
        await AuthorizeAdministrator(SpyUsers(session))(user_id)

    assert selects, "no SELECT on identity_user was captured"
    assert [s for s in selects if _FOR.search(s)] == []
