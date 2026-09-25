"""The `LogOut` use case: end the login a refresh token belongs to, if any (AC-11, I-29).

`presented` is a `TokenHash` or `None` — `None` when the request carried no refresh cookie at all.
Never a plaintext token (AC-10).
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import LoginRepository
from tailorcraft.domain.identity.value_objects import TokenHash
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


class LogOut:
    """Remove the login whose **current or retired** hash is `presented`; otherwise do nothing.

    Constructor arguments: the ports `logins`, `clock`, `events`.

    **Idempotent by construction** (AC-11): twice in a row, a `None`, an unknown hash, or a login a
    reuse revocation removed a moment ago — all return `None`, never an error. `LoginRepository.remove`
    is itself idempotent, so a logout racing another deletion cannot fail for having arrived second.

    Flow of `__call__`, with one `clock.now()`:

    1. `presented is None` → return.
    2. `login = await logins.find_by_current_token_hash(presented)`, else the login from
       `find_by_retired_token_hash(presented)`; neither → return.
    3. `login.record_logout(now)`; `await logins.remove(login.id)`;
       `await events.publish(*login.release_events())` (`LoggedOut`) — after the remove, never before.

    A retired token logs out the family too, and is **not** judged for reuse: the holder is asking
    for the login to end, which is what a reuse verdict would do anyway.
    """

    def __init__(
        self,
        logins: LoginRepository,
        clock: Clock,
        events: EventPublisherPort,
    ) -> None:
        self._logins = logins
        self._clock = clock
        self._events = events

    async def __call__(self, presented: TokenHash | None) -> None:
        if presented is None:
            return
        login = await self._logins.find_by_current_token_hash(presented)
        if login is None:
            found = await self._logins.find_by_retired_token_hash(presented)
            if found is None:
                return
            login, _generation = found
        login.record_logout(self._clock.now())
        await self._logins.remove(login.id)
        await self._events.publish(*login.release_events())
