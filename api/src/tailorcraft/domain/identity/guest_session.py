"""The `GuestSession` aggregate: an expiring, opaque, cookie-borne session that owns guest work.

**Explicitly not a login and not a weak one** (ADR-0008). It carries no password, no identity claim
and no upgrade path baked into its type — a future `User` aggregate is a separate concept entirely,
and the two do not share a base class even though they will eventually sit beside each other in the
same tables (CLAUDE.md: shared shape is not shared behaviour). What `GuestSession` authorizes is
narrow and mechanical: "the presenter of this cookie may act on rows whose `guest_session_id` links
to this id" — checked in the use case on every read, never inferred from possession of the id alone.

Invariant: `expires_at > created_at`. There is exactly one way to construct a valid instance
(`GuestSession.start`) and exactly one question it answers about itself (`is_expired`).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.errors import InvariantViolated


# NOT `slots=True`: this aggregate is later mapped by SQLAlchemy's *imperative* mapping
# (`registry.map_imperatively`), which instruments attributes on the instance `__dict__` — a
# `__slots__` class has none, so mapping would fail at wiring time, not at import time, which is a
# worse place to discover it. The private-attribute names below are exactly what the mapping in
# `infrastructure/persistence/mapping/identity/guest_session.py` targets, so renaming one here is a
# breaking change to that module too (ADR-0007).
class GuestSession:
    """An expiring, opaque session that owns guest-uploaded work.

    State: `_id`, `_token_hash`, `_created_at`, `_expires_at` — private, exposed only through
    read-only properties. There is no public setter; the one fact this aggregate can ever record
    about itself after construction is a question (`is_expired`), never a mutation.
    """

    # Class-level annotations only (no assignment): with no `__init__`, this is how mypy --strict
    # learns the attribute types that `start` will set directly on `self` and the properties below
    # will read back. SQLAlchemy's imperative mapping targets these exact names (ADR-0007).
    _id: GuestSessionId
    _token_hash: str
    _created_at: datetime
    _expires_at: datetime

    # No `__init__` override. `start` is the only way application code builds a valid instance —
    # calling `GuestSession(...)` directly falls through to `object.__init__`, which rejects any
    # keyword argument, so "the only constructor is `start`" is enforced by the absence of a
    # constructor here rather than by convention. SQLAlchemy's imperative mapping does not need
    # `__init__` either: it rehydrates a mapped instance by instrumenting `__dict__` directly and
    # never calls it on the load path (ADR-0007) — only application code calling `GuestSession(...)`
    # would, and that call is exactly what this omission blocks.

    @classmethod
    def start(
        cls,
        id: GuestSessionId,
        token_hash: str,
        at: datetime,
        ttl_hours: int,
    ) -> GuestSession:
        """The only way to create a `GuestSession`: `created_at = at`, `expires_at = at + ttl_hours`.

        Raises `InvariantViolated` if `ttl_hours` would not leave `expires_at > created_at` — in
        practice, if `ttl_hours <= 0`, which a caller should never pass but which this constructor
        does not trust.
        """
        if ttl_hours <= 0:
            raise InvariantViolated("ttl_hours must be > 0 (expires_at must be after created_at)")

        session = cls()
        session._id = id
        session._token_hash = token_hash
        session._created_at = at
        # `at` arrives already whole-second (the `Clock` port's contract, ADR-0007), and adding a
        # whole number of hours to a whole-second `datetime` cannot introduce a sub-second
        # component, so no defensive re-truncation is needed here.
        session._expires_at = at + timedelta(hours=ttl_hours)
        return session

    def is_expired(self, at: datetime) -> bool:
        """Whether `at` is at or past `expires_at`.

        The one behaviour this aggregate has. A `GET` on a base CV refuses an expired session
        outright (F-19); a `POST` is more forgiving and mints a fresh session instead (F-17/F-18) —
        that asymmetry lives in the use cases that call this method, not here.
        """
        return at >= self._expires_at

    @property
    def id(self) -> GuestSessionId:
        return self._id

    @property
    def token_hash(self) -> str:
        return self._token_hash

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def expires_at(self) -> datetime:
        return self._expires_at
