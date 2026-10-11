"""Persistence tests for `SqlAlchemyUserDirectoryQuery` (slice 4.2, T9, test-after; AC-15).

`tailorcraft_test` may hold users other tests left behind, so every assertion is scoped to the ids
(or the unique email token) this test created: a search by a per-test token isolates the seeded
users, and the unfiltered case asserts only on the seeded ids it can see.

Covers: the compiled statement (no `password_hash`, `strpos` and never `LIKE`, one statement per
page), a literal search for `%`, `_` and `\\`, newest-first order with a same-second tie broken by
id descending, `next_cursor` exactly when more exist, and a keyset that neither skips nor repeats
when an account is inserted between two page loads.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.user_directory import (
    EmailSearch,
    UserDirectoryCursor,
    UserDirectoryEntry,
    UserDirectoryPage,
)
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, Role, UserId
from tailorcraft.infrastructure.persistence.queries.user_directory import (
    SqlAlchemyUserDirectoryQuery,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)

_PHC = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
_BASE = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)


def _token() -> str:
    return uuid4().hex


async def _seed(
    session: AsyncSession,
    email: str,
    at: datetime,
    *,
    disabled_at: datetime | None = None,
    role: Role = Role.USER,
) -> UserId:
    users = SqlAlchemyUserRepository(session)
    user = User.register_with_password(
        users.next_identity(), EmailAddress.parse(email), PasswordHash(_PHC), at
    )
    if role is not Role.USER:
        user.change_role(role, at)
    if disabled_at is not None:
        user.disable(disabled_at, 0)
    user.release_events()
    await users.add(user)
    await session.flush()
    session.expunge_all()
    return user.id


def _search(raw: str) -> EmailSearch:
    parsed = EmailSearch.parse(raw)
    assert parsed is not None
    return parsed


def _ids(page: UserDirectoryPage) -> list[UserId]:
    return [entry.id for entry in page.entries]


@contextmanager
def _captured_statements(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = session.get_bind()

    def capture(conn: Connection, cursor: object, statement: str, *args: object) -> None:
        if statement.lstrip().upper().startswith("SELECT"):  # not the fixture's SAVEPOINTs
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


# --- the statement -------------------------------------------------------------------------------


async def test_a_page_is_one_statement_selecting_no_password_hash_and_using_strpos_not_like(
    session: AsyncSession,
) -> None:
    """AC-15 by statement capture, with a search and a cursor so every clause is in the text."""
    token = _token()
    await _seed(session, f"{token}@example.com", _BASE)
    query = SqlAlchemyUserDirectoryQuery(session)
    cursor = UserDirectoryCursor(created_at=_BASE + timedelta(days=1), user_id=UserId(uuid4()))

    with _captured_statements(session) as statements:
        await query.page(_search(token), cursor, 20)

    assert len(statements) == 1, statements
    sql = statements[0].lower()
    assert "password_hash" not in sql
    assert "like" not in sql
    assert "strpos" in sql
    assert "offset" not in sql
    assert "order by identity_user.created_at desc, identity_user.id desc" in sql


async def test_the_statement_without_search_or_cursor_has_neither_clause(
    session: AsyncSession,
) -> None:
    query = SqlAlchemyUserDirectoryQuery(session)

    with _captured_statements(session) as statements:
        await query.page(None, None, 5)

    assert len(statements) == 1
    sql = statements[0].lower()
    assert "strpos" not in sql
    assert "password_hash" not in sql


# --- entries -------------------------------------------------------------------------------------


async def test_an_entry_carries_every_directory_field(session: AsyncSession) -> None:
    token = _token()
    disabled_at = _BASE + timedelta(hours=1)
    user_id = await _seed(
        session, f"{token}@example.com", _BASE, disabled_at=disabled_at, role=Role.ADMIN
    )
    active_id = await _seed(session, f"{token}-active@example.com", _BASE + timedelta(seconds=1))

    page = await SqlAlchemyUserDirectoryQuery(session).page(_search(token), None, 20)

    by_id = {entry.id: entry for entry in page.entries}
    assert by_id[user_id] == UserDirectoryEntry(
        id=user_id,
        email=EmailAddress.parse(f"{token}@example.com"),
        role=Role.ADMIN,
        created_at=_BASE,
        disabled_at=disabled_at,
    )
    assert by_id[active_id].disabled_at is None
    assert by_id[active_id].role is Role.USER


async def test_no_match_is_an_empty_page_with_no_cursor(session: AsyncSession) -> None:
    page = await SqlAlchemyUserDirectoryQuery(session).page(_search(_token()), None, 20)

    assert page == UserDirectoryPage(entries=(), next_cursor=None)


# --- search --------------------------------------------------------------------------------------


async def test_search_matches_a_substring_anywhere_in_the_email(session: AsyncSession) -> None:
    token = _token()
    hit = await _seed(session, f"first.{token}.last@example.com", _BASE)
    await _seed(session, f"{_token()}@example.com", _BASE)

    page = await SqlAlchemyUserDirectoryQuery(session).page(_search(token[5:20]), None, 20)

    assert _ids(page) == [hit]


async def test_search_is_literal_for_percent_underscore_and_backslash(
    session: AsyncSession,
) -> None:
    token = _token()
    percent = await _seed(session, f"{token}100%off@example.com", _BASE)
    underscore = await _seed(session, f"{token}a_b@example.com", _BASE + timedelta(seconds=1))
    backslash = await _seed(session, f"{token}c\\d@example.com", _BASE + timedelta(seconds=2))
    decoys = [
        await _seed(session, f"{token}100xoff@example.com", _BASE + timedelta(seconds=3)),
        await _seed(session, f"{token}aXb@example.com", _BASE + timedelta(seconds=4)),
        await _seed(session, f"{token}cXd@example.com", _BASE + timedelta(seconds=5)),
    ]
    query = SqlAlchemyUserDirectoryQuery(session)

    assert _ids(await query.page(_search(f"{token}100%"), None, 20)) == [percent]
    assert _ids(await query.page(_search(f"{token}a_b"), None, 20)) == [underscore]
    assert _ids(await query.page(_search(f"{token}c\\d"), None, 20)) == [backslash]
    # A wildcard on its own must not widen into "every row".
    wide = await query.page(_search("%"), None, 50)
    assert not set(decoys) & set(_ids(wide))
    assert all("%" in entry.email.value for entry in wide.entries)


async def test_search_is_case_insensitive_through_the_normalized_value(
    session: AsyncSession,
) -> None:
    token = _token()
    user_id = await _seed(session, f"{token}@example.com", _BASE)

    page = await SqlAlchemyUserDirectoryQuery(session).page(_search(token.upper()), None, 20)

    assert _ids(page) == [user_id]


# --- order and paging ----------------------------------------------------------------------------


async def test_pages_are_newest_first_and_next_cursor_is_none_on_the_last_page(
    session: AsyncSession,
) -> None:
    token = _token()
    oldest = await _seed(session, f"{token}-1@example.com", _BASE)
    middle = await _seed(session, f"{token}-2@example.com", _BASE + timedelta(seconds=10))
    newest = await _seed(session, f"{token}-3@example.com", _BASE + timedelta(seconds=20))
    query = SqlAlchemyUserDirectoryQuery(session)

    full = await query.page(_search(token), None, 3)
    assert _ids(full) == [newest, middle, oldest]
    assert full.next_cursor is None, "exactly a full page with nothing after it has no cursor"

    first = await query.page(_search(token), None, 2)
    assert _ids(first) == [newest, middle]
    assert first.next_cursor == UserDirectoryCursor(
        created_at=_BASE + timedelta(seconds=10), user_id=middle
    )
    last = await query.page(_search(token), first.next_cursor, 2)
    assert _ids(last) == [oldest]
    assert last.next_cursor is None


async def test_a_same_second_tie_is_ordered_by_id_descending_without_skip_or_repeat(
    session: AsyncSession,
) -> None:
    token = _token()
    seeded = [await _seed(session, f"{token}-{i}@example.com", _BASE) for i in range(5)]
    expected = sorted(seeded, key=lambda uid: uid.value, reverse=True)
    query = SqlAlchemyUserDirectoryQuery(session)

    seen: list[UserId] = []
    cursor: UserDirectoryCursor | None = None
    for _ in range(10):
        page = await query.page(_search(token), cursor, 2)
        seen.extend(_ids(page))
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == expected


async def test_an_account_inserted_between_two_page_loads_appears_on_no_later_page(
    session: AsyncSession,
) -> None:
    token = _token()
    seeded = [
        await _seed(session, f"{token}-{i}@example.com", _BASE + timedelta(seconds=i))
        for i in range(5)
    ]
    expected_rest = list(reversed(seeded))
    query = SqlAlchemyUserDirectoryQuery(session)

    first = await query.page(_search(token), None, 2)
    newcomer = await _seed(session, f"{token}-new@example.com", _BASE + timedelta(seconds=99))
    rest: list[UserId] = []
    cursor = first.next_cursor
    while cursor is not None:
        page = await query.page(_search(token), cursor, 2)
        rest.extend(_ids(page))
        cursor = page.next_cursor

    assert _ids(first) == expected_rest[:2]
    assert _ids(first) + rest == expected_rest, "a page skipped or repeated an account"
    assert newcomer not in set(_ids(first)) | set(rest)


async def test_an_account_older_than_the_cursor_inserted_mid_paging_is_not_skipped(
    session: AsyncSession,
) -> None:
    """The mirror case: an insert *behind* the cursor is reached, so nothing is lost."""
    token = _token()
    newest = await _seed(session, f"{token}-a@example.com", _BASE + timedelta(seconds=30))
    middle = await _seed(session, f"{token}-b@example.com", _BASE + timedelta(seconds=20))
    query = SqlAlchemyUserDirectoryQuery(session)

    first = await query.page(_search(token), None, 1)
    older = await _seed(session, f"{token}-c@example.com", _BASE + timedelta(seconds=10))
    second = await query.page(_search(token), first.next_cursor, 5)

    assert _ids(first) == [newest]
    assert _ids(second) == [middle, older]
