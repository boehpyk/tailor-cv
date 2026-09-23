"""The `RefreshLogin` use case: rotate a refresh token, or find out that it was stolen (AC-10).

**SKELETON step (T12).** `__init__` is fully written and stores its arguments; `__call__`'s body is
deferred to T14.

Both arguments are `TokenHash`es: the route read the cookie, hashed it, minted the replacement and
hashed that too. **No plaintext token reaches this layer** (AC-10, ADR-0010), and `mypy` enforces it.

**Deliberately not idempotent.** Presenting one token twice is exactly the signal this use case
exists to detect; an honest client's second tab is served by the race grace (`REFRESH_RACE_GRACE`),
not by idempotence.

**One outcome must commit although it raises.** `RefreshTokenReused` is raised *after*
`logins.remove` — the router catches it inside the request and returns the 401 itself so the session
dependency commits the deletion (technical plan §2, I-24). That is the route's obligation; this use
case's is only to have removed and published before raising.
"""

from __future__ import annotations

from tailorcraft.application.identity.results import Authenticated
from tailorcraft.domain.identity.ports import AccessTokenPort, LoginRepository, UserRepository
from tailorcraft.domain.identity.value_objects import TokenHash
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


class RefreshLogin:
    """Exchange the presented refresh token for `replacement` (AC-10). Exactly five outcomes.

    Constructor arguments: the ports `logins`, `users`, `tokens`, `clock`, `events`. No lifetime —
    rotation never extends a login's absolute `expires_at` (OQ-9), and the access token's lifetime
    is `AccessTokenPort`'s.

    Flow of `__call__` (technical plan §2), with **one `clock.now()` for the whole call** — the
    expiry check, the rotation instant and the access token's `iat` are the same instant:

    1. `login = await logins.find_by_current_token_hash(presented)`. Found:
       - `retired = login.rotate(replacement, now)`; `LoginExpired` → `await logins.remove(login.id)`,
         raise `LoginNotFound` (**expired**, I-21 — deleted on sight).
       - `await logins.save_rotation(login, retired)`; `LoginConcurrentlyRotated` → raise
         `RefreshInProgress` (a lost race, I-25 — never a revocation).
       - `user = await users.get(login.user_id)`; `tokens.issue(user.id, now)`; return
         `Authenticated(user, login, access_token)` (**rotated**). No event: rotation records none.
    2. Else `await logins.find_by_retired_token_hash(presented)` → `(login, generation)`. Found:
       - `login.judge_retired(generation, now)`; `LoginExpired` → remove, raise `LoginNotFound`.
       - `RACED` → raise `RefreshInProgress`, nothing changed (**raced**, I-23).
       - `REUSED` → `await logins.remove(login.id)`; `await events.publish(*login.release_events())`
         (`RefreshTokenReuseDetected`); raise `RefreshTokenReused` (**reused**, I-24).
    3. Else raise `LoginNotFound`, nothing changed (**unknown**, I-20).
    """

    def __init__(
        self,
        logins: LoginRepository,
        users: UserRepository,
        tokens: AccessTokenPort,
        clock: Clock,
        events: EventPublisherPort,
    ) -> None:
        self._logins = logins
        self._users = users
        self._tokens = tokens
        self._clock = clock
        self._events = events

    async def __call__(self, presented: TokenHash, replacement: TokenHash) -> Authenticated:
        raise NotImplementedError
