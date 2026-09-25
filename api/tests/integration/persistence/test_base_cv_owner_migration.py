"""Migration tests for `1a2676aa3759` — "add owner and label to base cv" (T15, after, slice 2.2).

**AC-13.** The revision is purely additive against `66c9e18acc5c` — three nullable columns, two
CHECKs, and `guest_session_id` losing its `NOT NULL` — proven with the same up/down/up shape
`test_identity_migration.py` and `test_export_job_repository.py` use: a plain `def test_...` (never
`async def` — `alembic/env.py` opens its own `asyncio.run()`, which refuses to nest inside
pytest-asyncio's session-scoped loop while that loop is mid-test, the identical note every migration
test in this package carries), no `session`/`connection` fixture (those bind to a SAVEPOINT on the
shared connection, and Alembic's DDL must run outside of any such transaction), and unconditional
recovery back to head — this suite migrates its one test database to head once per session
(`conftest.py`'s `_migrated`), so a schema left below head here would silently break every test that
runs after this one, in this file and beyond. `1a2676aa3759` is real head at the time this file was
written, so — unlike `test_identity_migration.py`, which has to descend *past* this revision to reach
its own target — the down/up here is a single step.

**The refusing downgrade** is proved separately, against a genuinely committed user-owned row (the
migration's own connection is not the rolled-back `session` fixture's, so the row has to be real and
cleaned up by this file, not left to a transaction rollback).

**AC-14** — backward compatibility with the running 2.1 code — is proved as ordinary `async def`
tests against the already-migrated head schema (the `session`/`clock` fixtures, same as everywhere
else in this package): 2.1's insert shape still succeeds, and the two ways to violate "exactly one
owner" are recognised by `database.violated_constraint`, never by parsing the driver's message.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvStatus,
    CvContentType,
    OriginalFilename,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.settings import Settings

_TARGET_REVISION: Final = "1a2676aa3759"
_DOWN_REVISION: Final = "66c9e18acc5c"

_NEW_COLUMNS: Final[tuple[str, ...]] = ("user_id", "label", "copied_from_base_cv_id")
_NEW_CHECKS: Final[tuple[str, ...]] = (
    "ck_intake_base_cv_exactly_one_owner",
    "ck_intake_base_cv_label_only_when_user_owned",
)


async def _column_exists(url: str, table: str, column: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :column"
                ),
                {"table": table, "column": column},
            )
            return bool(result.scalar_one())
    finally:
        await engine.dispose()


async def _check_exists(url: str, table: str, name: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conrelid = to_regclass(:qualified) AND conname = :name"
                ),
                {"qualified": f"public.{table}", "name": name},
            )
            return bool(result.scalar_one())
    finally:
        await engine.dispose()


async def _guest_session_id_is_nullable(url: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'intake_base_cv' AND column_name = 'guest_session_id'"
                )
            )
            return bool(result.scalar_one() == "YES")
    finally:
        await engine.dispose()


# --- The migration itself: upgrade -> downgrade -> upgrade, with recovery if downgrade raises -----


@pytest.mark.usefixtures("_migrated")
def test_migration_1a2676aa3759_up_down_up_is_purely_additive(settings: Settings) -> None:
    """AC-13. Every new column and both CHECKs exist at head, disappear at `66c9e18acc5c`
    (`guest_session_id` reverting to `NOT NULL` with them), and reappear when head is restored.

    **Recovery is unconditional**, exactly as `test_identity_migration.py`'s and
    `test_export_job_repository.py`'s identical tests: a `downgrade()` that raises, an assertion that
    fails, or an `upgrade()` that itself raises during recovery all end by restoring head, or by
    chaining the recovery failure onto whatever it was trying to report — a failure recovering the
    schema must never silently replace the real assertion in the report, and must never be lost
    either.
    """
    url = settings.test_database_url
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)

    for column in _NEW_COLUMNS:
        assert asyncio.run(_column_exists(url, "intake_base_cv", column)) is True, (
            f"intake_base_cv.{column} must exist at head"
        )
    for check in _NEW_CHECKS:
        assert asyncio.run(_check_exists(url, "intake_base_cv", check)) is True, (
            f"intake_base_cv must carry {check} at head"
        )
    assert asyncio.run(_guest_session_id_is_nullable(url)) is True

    downgrade_error: Exception | None = None
    try:
        command.downgrade(config, _DOWN_REVISION)
    except Exception as exc:
        downgrade_error = exc

    if downgrade_error is not None:
        try:
            command.upgrade(config, "head")
            for column in _NEW_COLUMNS:
                assert asyncio.run(_column_exists(url, "intake_base_cv", column)) is True
        except Exception as recovery_exc:
            raise downgrade_error from recovery_exc
        raise downgrade_error

    assertion_error: AssertionError | None = None
    try:
        for column in _NEW_COLUMNS:
            assert asyncio.run(_column_exists(url, "intake_base_cv", column)) is False, (
                f"intake_base_cv.{column} must not exist below {_TARGET_REVISION} — downgrade() "
                "must drop it (AC-13)"
            )
        for check in _NEW_CHECKS:
            assert asyncio.run(_check_exists(url, "intake_base_cv", check)) is False, (
                f"intake_base_cv must not carry {check} below {_TARGET_REVISION}"
            )
        assert asyncio.run(_guest_session_id_is_nullable(url)) is False, (
            "guest_session_id must be restored to NOT NULL below the target revision"
        )
    except AssertionError as exc:
        assertion_error = exc

    try:
        command.upgrade(config, "head")
        for column in _NEW_COLUMNS:
            assert asyncio.run(_column_exists(url, "intake_base_cv", column)) is True, (
                "the schema must be back at head before the next test in the session runs"
            )
        assert asyncio.run(_guest_session_id_is_nullable(url)) is True
    except Exception as recovery_exc:
        if assertion_error is not None:
            raise assertion_error from recovery_exc
        raise

    if assertion_error is not None:
        raise assertion_error


# --- The refusing downgrade: a user-owned row must block it, never be deleted to make it fit --------


async def _insert_one_user_owned_row(url: str) -> tuple[UUID, UUID]:
    """A genuinely committed `identity_user` + `intake_base_cv` pair — this migration test runs
    outside the rolled-back `session` fixture (Alembic's DDL needs a connection with no open
    SAVEPOINT), so the row has to be real, on its own short-lived engine, and cleaned up by the
    caller rather than by a transaction rollback."""
    now = datetime.now(UTC).replace(microsecond=0)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            user_id = uuid7()
            await conn.execute(
                user_table.insert().values(
                    id=UserId(user_id),
                    email=EmailAddress.parse(f"downgrade-refusal-{user_id}@example.com"),
                    password_hash=PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"),
                    created_at=now,
                    password_updated_at=now,
                )
            )
            cv_id = uuid7()
            await conn.execute(
                base_cv_table.insert().values(
                    id=BaseCvId(cv_id),
                    guest_session_id=None,
                    user_id=UserId(user_id),
                    original_filename=OriginalFilename("refusal.pdf"),
                    content_type=CvContentType.PDF,
                    size_bytes=1,
                    file_key=FileRef.for_base_cv(BaseCvId(cv_id), CvContentType.PDF),
                    status=BaseCvStatus.UPLOADED,
                    uploaded_at=now,
                )
            )
        return user_id, cv_id
    finally:
        await engine.dispose()


async def _delete_user(url: str, user_id: UUID) -> None:
    """Deletes the user; `ON DELETE CASCADE` takes the saved CV with it."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(user_table.delete().where(user_table.c.id == UserId(user_id)))
    finally:
        await engine.dispose()


@pytest.mark.usefixtures("_migrated")
def test_downgrade_refuses_while_a_user_owned_saved_cv_exists(settings: Settings) -> None:
    """AC-13: the migration's own docstring promise, proven rather than trusted. With one
    user-owned `intake_base_cv` row committed, `downgrade()` raises the module's `_REFUSAL` sentence
    **before** touching any DDL (the migration locks the table and checks first), so the schema is
    still exactly at head afterward and the row is still there, untouched."""
    url = settings.test_database_url
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)

    user_id, cv_id = asyncio.run(_insert_one_user_owned_row(url))
    try:
        with pytest.raises(RuntimeError, match="refusing to downgrade 1a2676aa3759"):
            command.downgrade(config, _DOWN_REVISION)

        # Refused before any data was touched: the schema is still at head, and the row survives.
        for column in _NEW_COLUMNS:
            assert asyncio.run(_column_exists(url, "intake_base_cv", column)) is True, (
                f"a refused downgrade dropped intake_base_cv.{column} anyway"
            )
        assert asyncio.run(_guest_session_id_is_nullable(url)) is True, (
            "a refused downgrade touched guest_session_id's nullability anyway"
        )

        async def _row_still_there() -> bool:
            engine = create_async_engine(url, poolclass=NullPool)
            try:
                async with engine.connect() as conn:
                    result = await conn.execute(
                        text("SELECT count(*) FROM intake_base_cv WHERE id = :id"),
                        {"id": cv_id},
                    )
                    return bool(result.scalar_one())
            finally:
                await engine.dispose()

        assert asyncio.run(_row_still_there()) is True, (
            "the refused downgrade must never delete the row it refused over"
        )
    finally:
        asyncio.run(_delete_user(url, user_id))
        # No further recovery needed: a refusal raises before any DDL runs, so the schema was never
        # moved off head in the first place — unlike the up/down/up test above, there is nothing to
        # restore.
        assert asyncio.run(_column_exists(url, "intake_base_cv", "user_id")) is True


# --- AC-14: backward compatibility with the running 2.1 code, proved as ordinary async tests -------


async def test_2_1s_insert_shape_still_succeeds_against_the_migrated_schema(
    session: AsyncSession, clock: FixedClock
) -> None:
    """No `user_id`, a non-null `guest_session_id`, no label, no `copied_from_base_cv_id` — exactly
    what 2.1's `SqlAlchemyBaseCvRepository.add` (predating this migration) writes. The CHECK must not
    refuse a row it was designed to keep accepting."""
    sessions = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=sessions.next_identity(), token_hash="6f" * 32, at=clock.now(), ttl_hours=24
    )
    await sessions.add(owner)

    cv_id = BaseCvId(uuid7())
    await session.execute(
        base_cv_table.insert().values(
            id=cv_id,
            guest_session_id=owner.id,
            original_filename=OriginalFilename("legacy.pdf"),
            content_type=CvContentType.PDF,
            size_bytes=1,
            file_key=FileRef.for_base_cv(cv_id, CvContentType.PDF),
            status=BaseCvStatus.UPLOADED,
            uploaded_at=clock.now(),
        )
    )
    await session.flush()  # no IntegrityError


async def test_a_row_with_both_owners_is_refused_and_recognised_by_constraint_name(
    session: AsyncSession, clock: FixedClock
) -> None:
    sessions = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=sessions.next_identity(), token_hash="7a" * 32, at=clock.now(), ttl_hours=24
    )
    await sessions.add(owner)
    await session.flush()

    bogus_user_id = UserId(uuid7())
    cv_id = BaseCvId(uuid7())
    with pytest.raises(IntegrityError) as exc_info:
        await session.execute(
            base_cv_table.insert().values(
                id=cv_id,
                guest_session_id=owner.id,
                user_id=bogus_user_id,
                original_filename=OriginalFilename("both.pdf"),
                content_type=CvContentType.PDF,
                size_bytes=1,
                file_key=FileRef.for_base_cv(cv_id, CvContentType.PDF),
                status=BaseCvStatus.UPLOADED,
                uploaded_at=clock.now(),
            )
        )
    assert violated_constraint(exc_info.value) == "ck_intake_base_cv_exactly_one_owner"
    await session.rollback()


async def test_a_row_with_neither_owner_is_refused_and_recognised_by_constraint_name(
    session: AsyncSession, clock: FixedClock
) -> None:
    cv_id = BaseCvId(uuid7())
    with pytest.raises(IntegrityError) as exc_info:
        await session.execute(
            base_cv_table.insert().values(
                id=cv_id,
                guest_session_id=None,
                user_id=None,
                original_filename=OriginalFilename("neither.pdf"),
                content_type=CvContentType.PDF,
                size_bytes=1,
                file_key=FileRef.for_base_cv(cv_id, CvContentType.PDF),
                status=BaseCvStatus.UPLOADED,
                uploaded_at=clock.now(),
            )
        )
    assert violated_constraint(exc_info.value) == "ck_intake_base_cv_exactly_one_owner"
    await session.rollback()


async def test_violated_constraint_never_reveals_the_drivers_message(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The negative half of the two tests above: `violated_constraint`'s whole point is that a
    caller never has to parse `str(exc)` to tell one refusal from another — proven here by asserting
    the constraint *name* alone is what `SqlAlchemyBaseCvRepository.add`-style code would branch on,
    with no dependence on the withheld driver message's shape. Uses `GuestOwner` unreachable via the
    aggregate (`_guest_session_id`/`_user_id` are private, `BaseCv.upload` never allows both) — the
    same "insert around the domain" technique `test_base_cv_repository.py`'s CHECK-constraint probe
    and `test_user_repository.py`'s normalization probe both use to reach a state the aggregate
    itself cannot construct.
    """
    cv_id = BaseCvId(uuid7())
    with pytest.raises(IntegrityError) as exc_info:
        await session.execute(
            base_cv_table.insert().values(
                id=cv_id,
                guest_session_id=None,
                user_id=None,
                original_filename=OriginalFilename("silent.pdf"),
                content_type=CvContentType.PDF,
                size_bytes=1,
                file_key=FileRef.for_base_cv(cv_id, CvContentType.PDF),
                status=BaseCvStatus.UPLOADED,
                uploaded_at=clock.now(),
            )
        )
    name = violated_constraint(exc_info.value)
    assert name is not None
    assert name.startswith("ck_intake_base_cv_")
    await session.rollback()
