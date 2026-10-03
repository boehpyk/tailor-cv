"""`SqlAlchemyUserRepository` — the `UserRepository` port (ADR-0007, technical plan §3).

Filters query the **private** mapped attributes (`User._id`, `User._email`) through the typed casts
below, never `User.id` / `User.email` — those are read-only `@property` objects, and filtering on one
builds `WHERE true` or `WHERE false` silently. `repositories/identity/guest_session.py` has the full
account.

**`add` is the uniqueness check** (technical plan §0.4). There is no look-up before the insert: the
unique index `uq_identity_user_email` referees two concurrent registrations atomically, which a
`SELECT` first cannot (AC-16). This module's job is to turn that index's refusal — and only that
index's — into `EmailAlreadyRegistered`, and to do it without poisoning the request's transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import structlog
from sqlalchemy import literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from tailorcraft.domain.identity.errors import EmailAlreadyRegistered, UserNotFound
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import UserRepository

log = structlog.get_logger(__name__)

# The constraint `add` translates, by name — declared in `mapping/identity/user.py` as the column's
# `unique=True` and rendered by `registry.py`'s naming convention. Renaming it there without
# renaming it here would turn every duplicate registration into a re-raised `IntegrityError` (a
# 503), which is loud; the test for AC-16 is what keeps it from being discovered in production.
_EMAIL_UNIQUE: Final = "uq_identity_user_email"

# Typed views of the mapped private attributes, for the reason the guest-session repository's
# identical casts carry: mypy otherwise sees the domain type and `==` type-checks as a plain `bool`.
_USER_ID: InstrumentedAttribute[UserId] = cast("InstrumentedAttribute[UserId]", User._id)
_USER_EMAIL: InstrumentedAttribute[EmailAddress] = cast(
    "InstrumentedAttribute[EmailAddress]", User._email
)


class SqlAlchemyUserRepository:
    """Persistence for `User`, backed by `identity_user`.

    Hides its `AsyncSession` completely (ADR-0007). The unit of work is the caller's: one transaction
    per request, opened in `infrastructure/api/deps.py::get_session`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> UserId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return UserId(uuid7())

    async def add(self, user: User) -> None:
        """Insert `user`; raise `EmailAlreadyRegistered` if the normalized email is taken.

        **Inside a SAVEPOINT, so a refusal leaves the request's transaction usable.** A failed flush
        rolls back to the nearest transaction boundary; at the root that is the whole request, and
        every instance the session holds is expired on the way (CLAUDE.md, the 1.4 lesson). The
        router still has to answer a clean 409 with a working session, so the boundary it rolls back
        to is this `begin_nested()`, and what it discards is this one pending `User`.

        `add` happens **inside** the `async with`, never before it: `begin_nested()` flushes the
        session on entry, so a `User` added first would be inserted *outside* the SAVEPOINT and its
        violation would take the root transaction with it.

        **Recognised by constraint name, never by message** (`violated_constraint`). A different
        refusal — `ck_identity_user_email_normalized` from a value that somehow bypassed
        `EmailAddress`, a primary-key collision — is not "email taken", and translating it as one
        would tell a user their address is registered when it is not. Those are re-raised.

        The id is read into a local **before** the flush. The SAVEPOINT limits the expiry to the
        failed `User` itself, but that is exactly the object whose id the log line below needs, and
        after the nested rollback it has been expunged — reading `user.id` then would work today only
        because the attribute happened not to be expired. A local is immune to the question.
        """
        user_id = user.id
        try:
            async with self._session.begin_nested():
                self._session.add(user)
                await self._session.flush()
        except IntegrityError as exc:
            constraint = violated_constraint(exc)
            if constraint == _EMAIL_UNIQUE:
                # `from None`: the chain holds nothing after the engine's listener, but the frame
                # holds `user`, whose email is PII (Constitution §8).
                raise EmailAlreadyRegistered() from None
            # Not the email. The id and the constraint's name only — never the row, and the
            # exception's own message was already reduced to identifiers by the listener.
            log.warning(
                "identity.user_insert_refused",
                user_id=str(user_id.value),
                constraint_name=constraint,
                error_type=type(exc).__name__,
            )
            raise

    async def get(self, user_id: UserId) -> User:
        result = await self._session.execute(
            select(User).where(_USER_ID == user_id)  # noqa: SIM300 -- column first, see the casts
        )
        found = result.scalar_one_or_none()
        if found is None:
            # `from None` for the reason `SqlAlchemyTailoringRunRepository.get` gives. The message
            # names the id, which is not PII and is what I-39's log line needs.
            raise UserNotFound(f"no User with id {user_id!r}") from None
        return found

    async def find_by_email(self, email: EmailAddress) -> User | None:
        """One seek on `uq_identity_user_email`. The argument is an `EmailAddress`, so it is already
        normalized, and it compares equal to the stored value by construction."""
        result = await self._session.execute(
            select(User).where(_USER_EMAIL == email)  # noqa: SIM300 -- column first, see the casts
        )
        return result.scalar_one_or_none()

    async def save(self, user: User) -> None:
        """Flush a change to a loaded user — today only the rehash on login (I-11).

        A plain flush, with no SAVEPOINT and no translation: the only column that changes is the
        password hash and its instant, neither of which carries a constraint a correct aggregate can
        violate. A failure here is a fault, and it propagates as one (already stripped of its data by
        the engine's listener). `add()` re-attaches a user that became detached, as the tailoring
        repository's `save` does; for a loaded one it is a no-op.
        """
        self._session.add(user)
        await self._session.flush()

    async def get_for_update(self, user_id: UserId) -> User:
        """`get` under `SELECT … FOR UPDATE`: the user row is locked exclusively until the end of the
        current transaction (technical plan §0.7, §0.8). Raises `UserNotFound`.

        **`populate_existing`, because the lock is the point of the read.** A `select()` whose row is
        already in the identity map hands back the mapped instance with its *loaded* attributes kept
        (CLAUDE.md, 2.4's `d37481f`). The statement waits for any concurrent writer and then sees the
        latest committed row, but without `populate_existing` that row would be discarded in favour of
        whatever this session read before it waited — a `ResetPassword` would then act on a hash a
        concurrent change had already replaced. With it, the instance is overwritten from the row the
        lock was taken on.

        Overwriting is safe here because nothing in the session should hold an unflushed change to
        this user: `ResetPassword` takes this lock before it touches the aggregate. A caller that
        dirtied the user first would have that change autoflushed by this very statement, which is
        the ordinary ORM behaviour and not a lost update.
        """
        result = await self._session.execute(
            select(User)
            .where(_USER_ID == user_id)  # noqa: SIM300 -- column first, see the casts
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        found = result.scalar_one_or_none()
        if found is None:
            raise UserNotFound(f"no User with id {user_id!r}") from None
        return found

    async def confirm_credential_unchanged(self, user_id: UserId, seen: PasswordHash) -> bool:
        """`SELECT 1 FROM identity_user WHERE id = :u AND password_hash = :seen FOR SHARE`.

        **The shared lock is held until the end of the current transaction** — that is what makes a
        reset's `get_for_update` wait for this login to commit, so the reset then deletes the `Login`
        it wrote (technical plan §0.7, ADR-0028). Only `LogIn` calls it: account deletion re-checks
        under `get_for_update` instead, since a shared lock upgraded to an exclusive one by two
        concurrent deletions deadlocks.

        Core, against the table, never the ORM: the answer must come from the row as it is *now*,
        and an identity-map hit would answer from the hash this session loaded before verifying —
        the very value being checked. Under READ COMMITTED a `FOR SHARE` that waited on a concurrent
        writer re-evaluates its `WHERE` against the newly committed row, so a reset that committed
        while this waited yields no row → `False`. No row also when the user is gone.

        Compared on the hash, which `PasswordHashType` binds exactly as it stores it; the hash is a
        credential and is never logged here.
        """
        result = await self._session.execute(
            select(literal(1))
            .select_from(user_table)
            .where(user_table.c.id == user_id, user_table.c.password_hash == seen)
            .with_for_update(read=True)
        )
        return result.first() is not None


if TYPE_CHECKING:
    # Makes mypy prove the class structurally satisfies the port rather than trusting the shape by
    # eye. Never executed; costs nothing at runtime and adds no import cycle.
    def _assert_implements_user_repository(repo: SqlAlchemyUserRepository) -> None:
        _: UserRepository = repo
