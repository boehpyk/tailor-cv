"""Value objects for the `identity` bounded context.

`identity` is deliberately small in this slice: a guest session is not a login, and shares nothing
with the future `User` (ADR-0008) — see `docs/adr/0008-*` for why the two are not unified under a
base class even though they will eventually sit side by side on the same tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class GuestSessionId:
    """A `GuestSession`'s identity, typed for the same reason `BaseCvId` is: so a repository method
    or a use case argument cannot silently accept a `BaseCvId` where a session id was meant. The
    authorization rule this slice depends on — `intake_base_cv.guest_session_id == the resolved
    session id` — is exactly the kind of check that a bare `UUID` would let slip past `mypy`."""

    value: UUID

    def __post_init__(self) -> None:
        raise NotImplementedError
