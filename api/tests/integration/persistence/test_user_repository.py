"""Persistence tests for `User` — the imperative mapping, `EmailAddressType`/`PasswordHashType` and
`SqlAlchemyUserRepository` against real PostgreSQL (T22, written **after**: this shape is discovered
against SQLAlchemy, not designed ahead of it — technical-plan.md's Test plan).

Mirrors `test_guest_session_repository.py`'s structure: mapping round-trip with
`session.expunge_all()` to force a genuine reload, whole-second timestamp fidelity. Two things are
specific to `identity_user` and have no analogue in that sibling file:

- **The unique index is the registration race's referee** (technical-plan.md §0.4, AC-16): `add()`
  never looks the email up first, so the only proof that duplicates are refused is a real unique
  violation reaching a real `EmailAlreadyRegistered`, with the request's transaction still usable
  afterwards — `begin_nested()`'s whole point (`repositories/identity/user.py`'s own docstring).
- **`ck_identity_user_email_normalized` is a belt for a value object that already refuses to exist
  un-normalized** (AC-2). `EmailAddress.__post_init__` makes `EmailAddress("Alex@x.io")` impossible
  through ordinary code, so reaching the CHECK needs either a raw `INSERT` that never touched the
  domain, or a `User` whose `_email` has been corrupted directly with `object.__setattr__` past the
  frozen dataclass's own constructor — the implementer's suggested technique, used below. The second
  path is the one that proves `SqlAlchemyUserRepository.add` does not mistranslate a CHECK violation
  into `EmailAlreadyRegistered`, which would tell a user their address is registered when it is not.

**AC-18.** Every test below that forces an `IntegrityError` plants a marker email and a marker
password-hash fragment and asserts both are absent from `"".join(traceback.format_exception(exc))`
— what Celery logs and what Sentry's chained-exception walker renders — and, for good measure, from
`str(exc)`. The traceback is the assertion CLAUDE.md names; `str(exc)` alone would pass a
`hide_parameters=True`-only fix that leaves the chained driver exception's own message intact
(`test_database_engine.py`'s own finding, repeated here for `identity_user`'s two credential-shaped
columns).
"""

from __future__ import annotations

import logging
import traceback
from typing import Final
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.errors import EmailAlreadyRegistered
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.settings import Settings

_MARKER_EMAIL_LOCAL: Final = "qa22identitymarker8f2c1d"
_MARKER_HASH_FRAGMENT: Final = "QA22MARKERHASH5e19a3"


def _hash(fragment: str = "") -> PasswordHash:
    return PasswordHash(f"$argon2id$v=19$m=65536,t=3,p=4$c2FsdA${fragment or 'aGFzaA'}")


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    """Without this, `caplog` only captures structlog output if some earlier test in the session
    happened to configure logging first (`test_purge_privacy_log_markers.py`'s identical fixture,
    same reason)."""
    configure_logging(settings)


# --- Round trip --------------------------------------------------------------------------------


async def test_round_trip_preserves_value_object_types_and_whole_second_timestamps(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyUserRepository(session)
    user_id = repo.next_identity()
    email = EmailAddress.parse("Alex@Example.com")
    password_hash = _hash()
    user = User.register_with_password(
        id=user_id, email=email, password_hash=password_hash, at=clock.now()
    )
    await repo.add(user)

    session.expunge_all()
    reloaded = await repo.get(user_id)

    assert isinstance(reloaded.id, UserId)
    assert reloaded.id == user_id
    assert isinstance(reloaded.email, EmailAddress)
    assert reloaded.email == email
    assert reloaded.email.value == "alex@example.com"
    assert isinstance(reloaded.password_hash, PasswordHash)
    assert reloaded.password_hash == password_hash
    assert reloaded.created_at == user.created_at
    assert reloaded.password_updated_at == user.password_updated_at
    assert reloaded.created_at.microsecond == 0
    assert reloaded.password_updated_at.microsecond == 0


async def test_get_raises_user_not_found_for_an_unknown_id(session: AsyncSession) -> None:
    from tailorcraft.domain.identity.errors import UserNotFound

    repo = SqlAlchemyUserRepository(session)
    with pytest.raises(UserNotFound):
        await repo.get(repo.next_identity())


async def test_find_by_email_returns_none_when_no_user_matches(session: AsyncSession) -> None:
    repo = SqlAlchemyUserRepository(session)
    assert await repo.find_by_email(EmailAddress.parse("nobody@example.com")) is None


async def test_find_by_email_returns_the_matching_user_for_a_differently_cased_query(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`EmailAddress` equality *is* the uniqueness rule (technical-plan.md §1): a lookup with a
    differently-cased-but-equal address must find the row, because both normalize to the same
    string the unique index compares."""
    repo = SqlAlchemyUserRepository(session)
    user = User.register_with_password(
        id=repo.next_identity(),
        email=EmailAddress.parse("Jordan@Example.com"),
        password_hash=_hash(),
        at=clock.now(),
    )
    await repo.add(user)

    found = await repo.find_by_email(EmailAddress.parse(" JORDAN@example.com "))
    assert found is not None
    assert found.id == user.id


async def test_save_persists_a_rehashed_password(session: AsyncSession, clock: FixedClock) -> None:
    repo = SqlAlchemyUserRepository(session)
    user = User.register_with_password(
        id=repo.next_identity(),
        email=EmailAddress.parse("rehash@example.com"),
        password_hash=_hash("old"),
        at=clock.now(),
    )
    await repo.add(user)

    new_hash = _hash("new")
    clock.advance(60)
    user.replace_password_hash(new_hash, clock.now())
    await repo.save(user)

    session.expunge_all()
    reloaded = await repo.get(user.id)
    assert reloaded.password_hash == new_hash
    assert reloaded.password_updated_at == user.password_updated_at
    assert reloaded.password_updated_at > reloaded.created_at


# --- The unique index: the registration race's referee (AC-16, technical-plan.md §0.4) ---------


async def test_registering_a_case_and_whitespace_variant_of_a_taken_email_raises_email_already_registered(
    session: AsyncSession, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    repo = SqlAlchemyUserRepository(session)
    first = User.register_with_password(
        id=repo.next_identity(),
        email=EmailAddress.parse("Casey@Example.com"),
        password_hash=_hash(),
        at=clock.now(),
    )
    await repo.add(first)

    duplicate = User.register_with_password(
        id=repo.next_identity(),
        email=EmailAddress.parse(" casey@example.COM "),
        password_hash=_hash(),
        at=clock.now(),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(EmailAlreadyRegistered):
        await repo.add(duplicate)

    # Duplicate email logs nothing from the repository — the caller already knows the email, and
    # the use case's own log line (not this repository's) is where I-5's `reason=email_taken` comes
    # from.
    assert "identity.user_insert_refused" not in caplog.text

    # The request's transaction is still usable: `add()` contains the refusal in a SAVEPOINT
    # (`begin_nested()`), never the whole unit of work — a different, unrelated registration
    # succeeds right after.
    third = User.register_with_password(
        id=repo.next_identity(),
        email=EmailAddress.parse("dakota@example.com"),
        password_hash=_hash(),
        at=clock.now(),
    )
    await repo.add(third)

    normalized = EmailAddress.parse("casey@example.com")
    rows = await session.execute(select(user_table.c.email).where(user_table.c.email == normalized))
    matching = rows.scalars().all()
    assert matching == [normalized], "exactly one row for the normalized email"


async def test_two_registrations_of_the_same_normalized_email_leave_exactly_one_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The structural half of AC-16 (the concurrency half — two real, concurrent connections — is
    T31's job): even sequentially, a second `add()` of the same normalized email must never produce
    a second row, whatever raised."""
    repo = SqlAlchemyUserRepository(session)
    email = EmailAddress.parse("river@example.com")
    await repo.add(
        User.register_with_password(
            id=repo.next_identity(), email=email, password_hash=_hash(), at=clock.now()
        )
    )

    with pytest.raises(EmailAlreadyRegistered):
        await repo.add(
            User.register_with_password(
                id=repo.next_identity(), email=email, password_hash=_hash(), at=clock.now()
            )
        )

    count = await session.execute(select(user_table.c.id).where(user_table.c.email == email))
    assert len(count.scalars().all()) == 1


# --- The CHECK constraint: a belt for a VO that already refuses to exist un-normalized (AC-2) ---


async def test_a_raw_insert_of_an_un_normalized_email_is_refused_by_the_database_check(
    session: AsyncSession, clock: FixedClock
) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        await session.execute(
            text(
                "INSERT INTO identity_user (id, email, password_hash, created_at, "
                "password_updated_at) VALUES (:id, :email, :password_hash, :at, :at)"
            ),
            {
                "id": uuid4(),
                "email": "Alex@x.io",
                "password_hash": _hash().value,
                "at": clock.now(),
            },
        )
    assert "ck_identity_user_email_normalized" in str(exc_info.value)
    await session.rollback()


async def test_a_check_violation_reached_through_the_repository_is_re_raised_never_translated(
    session: AsyncSession, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """Constructs a `User` normally, then corrupts its `_email` past the frozen `EmailAddress`
    value object's own constructor — the only way to reach a state `ck_identity_user_email_normalized`
    exists to refuse, since every ordinary code path normalizes before an `EmailAddress` can exist at
    all. `SqlAlchemyUserRepository.add` must re-raise the resulting `IntegrityError` as-is: recognising
    it as anything but `uq_identity_user_email` and calling it `EmailAlreadyRegistered` would tell a
    user their address is registered when it never was (the repository's own docstring)."""
    repo = SqlAlchemyUserRepository(session)
    user_id = repo.next_identity()
    marker_email = f"{_MARKER_EMAIL_LOCAL}-check@example.com"
    marker_hash = _hash(_MARKER_HASH_FRAGMENT)
    user = User.register_with_password(
        id=user_id,
        email=EmailAddress.parse(marker_email),
        password_hash=marker_hash,
        at=clock.now(),
    )
    corrupted_email = object.__new__(EmailAddress)
    object.__setattr__(corrupted_email, "value", "Marker@x.io")
    user._email = corrupted_email  # bypass the VO — see the docstring above

    with caplog.at_level(logging.WARNING), pytest.raises(IntegrityError) as exc_info:
        await repo.add(user)

    assert not isinstance(exc_info.value, EmailAlreadyRegistered), (
        "a CHECK violation must be re-raised as IntegrityError, never translated to "
        "EmailAlreadyRegistered"
    )
    exc = exc_info.value
    rendered = "".join(traceback.format_exception(exc))
    assert "ck_identity_user_email_normalized" in str(exc)
    assert "Marker@x.io" not in str(exc)
    assert "Marker@x.io" not in rendered
    assert _MARKER_HASH_FRAGMENT not in str(exc)
    assert _MARKER_HASH_FRAGMENT not in rendered

    # The repository's own log line: ids and the constraint's name only (implementer's note).
    assert "identity.user_insert_refused" in caplog.text
    assert str(user_id.value) in caplog.text
    assert "ck_identity_user_email_normalized" in caplog.text
    assert "IntegrityError" in caplog.text
    assert "Marker@x.io" not in caplog.text
    assert marker_email not in caplog.text
    assert _MARKER_HASH_FRAGMENT not in caplog.text

    # The SAVEPOINT contains the refusal — the session is immediately usable again.
    another = User.register_with_password(
        id=repo.next_identity(),
        email=EmailAddress.parse("unaffected@example.com"),
        password_hash=_hash(),
        at=clock.now(),
    )
    await repo.add(another)


# --- AC-18: a marker email and a marker password-hash fragment never leak through a traceback ---


async def test_ac18_a_raw_unique_violation_leaks_neither_the_email_nor_the_hash(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyUserRepository(session)
    marker_email = f"{_MARKER_EMAIL_LOCAL}-unique@example.com"
    marker_hash = _hash(_MARKER_HASH_FRAGMENT)
    await repo.add(
        User.register_with_password(
            id=repo.next_identity(),
            email=EmailAddress.parse(marker_email),
            password_hash=marker_hash,
            at=clock.now(),
        )
    )

    with pytest.raises(IntegrityError) as exc_info:
        await session.execute(
            text(
                "INSERT INTO identity_user (id, email, password_hash, created_at, "
                "password_updated_at) VALUES (:id, :email, :password_hash, :at, :at)"
            ),
            {
                "id": uuid4(),
                "email": marker_email,
                "password_hash": marker_hash.value,
                "at": clock.now(),
            },
        )

    exc = exc_info.value
    rendered = "".join(traceback.format_exception(exc))
    assert marker_email not in str(exc)
    assert marker_email not in rendered
    assert _MARKER_HASH_FRAGMENT not in str(exc)
    assert _MARKER_HASH_FRAGMENT not in rendered

    await session.rollback()


async def test_ac18_a_raw_primary_key_violation_leaks_neither_the_email_nor_the_hash(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyUserRepository(session)
    marker_email = f"{_MARKER_EMAIL_LOCAL}-pk@example.com"
    marker_hash = _hash(_MARKER_HASH_FRAGMENT)
    user = User.register_with_password(
        id=repo.next_identity(),
        email=EmailAddress.parse(marker_email),
        password_hash=marker_hash,
        at=clock.now(),
    )
    await repo.add(user)

    other_marker_email = f"{_MARKER_EMAIL_LOCAL}-pk-collider@example.com"
    with pytest.raises(IntegrityError) as exc_info:
        await session.execute(
            text(
                "INSERT INTO identity_user (id, email, password_hash, created_at, "
                "password_updated_at) VALUES (:id, :email, :password_hash, :at, :at)"
            ),
            {
                "id": user.id.value,
                "email": other_marker_email,
                "password_hash": marker_hash.value,
                "at": clock.now(),
            },
        )

    exc = exc_info.value
    rendered = "".join(traceback.format_exception(exc))
    assert other_marker_email not in str(exc)
    assert other_marker_email not in rendered
    assert _MARKER_HASH_FRAGMENT not in str(exc)
    assert _MARKER_HASH_FRAGMENT not in rendered

    await session.rollback()


async def test_ac18_the_translated_email_already_registered_error_leaks_neither_the_email_nor_the_hash(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyUserRepository(session)
    marker_email = f"{_MARKER_EMAIL_LOCAL}-dup@example.com"
    marker_hash = _hash(_MARKER_HASH_FRAGMENT)
    await repo.add(
        User.register_with_password(
            id=repo.next_identity(),
            email=EmailAddress.parse(marker_email),
            password_hash=marker_hash,
            at=clock.now(),
        )
    )

    with pytest.raises(EmailAlreadyRegistered) as exc_info:
        await repo.add(
            User.register_with_password(
                id=repo.next_identity(),
                email=EmailAddress.parse(marker_email),
                password_hash=_hash(_MARKER_HASH_FRAGMENT + "-second"),
                at=clock.now(),
            )
        )

    exc = exc_info.value
    rendered = "".join(traceback.format_exception(exc))
    assert marker_email not in str(exc)
    assert marker_email not in rendered
    assert _MARKER_HASH_FRAGMENT not in rendered
