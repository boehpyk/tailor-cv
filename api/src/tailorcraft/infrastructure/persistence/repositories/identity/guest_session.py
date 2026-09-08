"""`SqlAlchemyGuestSessionRepository` — the `GuestSessionRepository` port (ADR-0007).

Filters below query `GuestSession._id` / `GuestSession._token_hash`, the **private** attributes the
imperative mapping in `infrastructure/persistence/mapping/identity/guest_session.py` targets — never
`GuestSession.id` / `GuestSession.token_hash`. Those short names are plain read-only `@property`
objects on the domain class, not `InstrumentedAttribute`s: `select(GuestSession).where(
GuestSession.id == x)` would call the property, get back a `GuestSessionId`, evaluate a bare Python
`==` against the id argument, and build `select(...).where(True)` or `.where(False)` — a predicate
that filters nothing (or everything), silently. This looks like a typo the first time you see it; it
is not one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from tailorcraft.domain.identity.errors import GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.infrastructure.identifiers import uuid7

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import GuestSessionRepository

# `GuestSession._id` / `._token_hash` are class-body annotations only — `map_imperatively`'s
# `properties=` installs the real `InstrumentedAttribute` at import time, not at class-definition
# time — so mypy sees them typed as the *domain* type (`GuestSessionId`, `str`), not as SQLAlchemy's
# mapped attribute. `GuestSession._id == session_id` would then type-check as a plain `bool`
# (`GuestSessionId.__eq__`), which is exactly the runtime trap the module docstring describes, just
# caught by mypy instead of a silently-empty query. These `cast`s tell mypy what is actually there at
# runtime without touching behaviour; each is a `cast`, not an `Any` (CLAUDE.md bans unjustified
# `Any`, not a `cast` with a reason attached, which this comment is).
_GUEST_SESSION_ID: InstrumentedAttribute[GuestSessionId] = cast(
    "InstrumentedAttribute[GuestSessionId]", GuestSession._id
)
_GUEST_SESSION_TOKEN_HASH: InstrumentedAttribute[str] = cast(
    "InstrumentedAttribute[str]", GuestSession._token_hash
)


class SqlAlchemyGuestSessionRepository:
    """Persistence for `GuestSession`, backed by `identity_guest_session`.

    Hides its `AsyncSession` completely — nothing outside this module ever sees it (ADR-0007). The
    unit of work (commit/rollback) is the caller's concern, opened once per request in
    `infrastructure/api/deps.py::get_session`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> GuestSessionId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return GuestSessionId(uuid7())

    async def add(self, session: GuestSession) -> None:
        self._session.add(session)
        # `flush()`, not `commit()`: the transaction boundary belongs to the caller (a request or a
        # task), not to the repository. Flushing makes the insert visible to later statements in the
        # *same* transaction (e.g. a subsequent `get` in the same use case) without ending it.
        await self._session.flush()

    async def get(self, session_id: GuestSessionId) -> GuestSession:
        # `_GUEST_SESSION_ID`, not `GuestSession.id` — see the module docstring: `.id` is a
        # read-only property, not an InstrumentedAttribute, and filtering on it builds no SQL
        # predicate at all.
        result = await self._session.execute(
            select(GuestSession).where(_GUEST_SESSION_ID == session_id)  # noqa: SIM300 -- must stay column-op-first: `GuestSessionId.__eq__` (dataclass) shadows SQLAlchemy's operator overload if `session_id` is on the left, turning this into a Python bool again (see block comment above)
        )
        found = result.scalar_one_or_none()
        if found is None:
            raise GuestSessionNotFound(f"no GuestSession with id {session_id!r}")
        return found

    async def find_by_token_hash(self, token_hash: str) -> GuestSession | None:
        result = await self._session.execute(
            select(GuestSession).where(_GUEST_SESSION_TOKEN_HASH == token_hash)  # noqa: SIM300 -- same reason as `get` above: keep the InstrumentedAttribute on the left
        )
        return result.scalar_one_or_none()


if TYPE_CHECKING:
    # Makes mypy prove `SqlAlchemyGuestSessionRepository` structurally satisfies
    # `GuestSessionRepository` rather than trusting the shape by eye. Never executed — a `Protocol`
    # needs no instance to check against, only a compatible signature — so it costs nothing at
    # runtime and adds no import cycle (the port module is imported for typing only).
    def _assert_implements_guest_session_repository(
        repo: SqlAlchemyGuestSessionRepository,
    ) -> None:
        _: GuestSessionRepository = repo
