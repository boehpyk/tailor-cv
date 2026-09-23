"""The `RegisterUser` use case: create an account and sign its owner in, in one unit of work (AC-8).

**No plaintext refresh token reaches this layer.** The route mints `(token, hash)` and passes only
`refresh_token_hash` (ADR-0010's pattern for the guest cookie, AC-10). The plaintext *password* does
arrive — hashing it is the point — and is wrapped in `Password` on the first line, which cannot be
printed.

**This layer does not log** (the house rule `application/export/render_export_job.py` writes out).
The facts worth recording leave as the events the aggregates record, published after the saves.
"""

from __future__ import annotations

from datetime import timedelta

from tailorcraft.application.identity.results import Authenticated
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.ports import (
    AccessTokenPort,
    LoginRepository,
    PasswordHasherPort,
    UserRepository,
)
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    Password,
    PasswordPolicy,
    TokenHash,
)
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


class RegisterUser:
    """Create a `User` and its first `Login`, and issue an access token (AC-8, I-1 … I-6).

    Constructor arguments:

    - `users`, `logins`, `hasher`, `tokens`, `clock`, `events` — ports, bound in `deps.py`.
    - `policy` — the `PasswordPolicy` registration applies. **Injected, not constructed here**, so the
      one object that refuses a password is the one whose `min_length`/`max_length` the 422 envelope
      reports (technical plan §1: the policy is data). Registration only: `LogIn` never takes one.
    - `refresh_lifetime` — the `Login`'s absolute lifetime (`Login.start(lifetime=…)`), a `timedelta`
      the composition root builds from `settings.refresh_token_ttl_days`. A `timedelta` rather than
      an `int` of days so the unit is a type, not a parameter name. The **access** token's lifetime
      is not here: it belongs to `AccessTokenPort`, which reports it on `IssuedAccessToken`.

    Flow of `__call__` (technical plan §2), with **one `clock.now()` for the whole call**:

    1. `EmailAddress.parse(raw_email)` → `Password.from_input(raw_password)` →
       `policy.check(password, email)`. Each may raise (`InvalidEmailAddress`, `WeakPassword`)
       **before the hasher is called** — a recording hasher sees zero calls on I-1 … I-4.
    2. `await hasher.hash(password)` (`PasswordHashingFailed` propagates).
    3. `User.register_with_password(users.next_identity(), email, hash, now)`;
       `await users.add(user)` — **the insert is the uniqueness check**; `EmailAlreadyRegistered`
       propagates, after exactly one hash, and there is never a `find_by_email` first (§0.4).
    4. `Login.start(logins.next_identity(), user.id, refresh_token_hash, now, refresh_lifetime)`;
       `await logins.add(login)`.
    5. `tokens.issue(user.id, now)`.
    6. `await events.publish(*user.release_events(), *login.release_events())` — after both adds,
       never before — and return `Authenticated(user, login, access_token)`.

    The transaction is the request's (`deps.get_session`): user row, login row and events are
    all-or-nothing.
    """

    def __init__(
        self,
        users: UserRepository,
        logins: LoginRepository,
        hasher: PasswordHasherPort,
        tokens: AccessTokenPort,
        clock: Clock,
        events: EventPublisherPort,
        policy: PasswordPolicy,
        refresh_lifetime: timedelta,
    ) -> None:
        self._users = users
        self._logins = logins
        self._hasher = hasher
        self._tokens = tokens
        self._clock = clock
        self._events = events
        self._policy = policy
        self._refresh_lifetime = refresh_lifetime

    async def __call__(
        self,
        raw_email: str,
        raw_password: str,
        refresh_token_hash: TokenHash,
    ) -> Authenticated:
        email = EmailAddress.parse(raw_email)
        password = Password.from_input(raw_password)
        self._policy.check(password, email)
        password_hash = await self._hasher.hash(password)
        now = self._clock.now()

        user = User.register_with_password(self._users.next_identity(), email, password_hash, now)
        # The insert is the uniqueness check (§0.4): no `find_by_email` first, so a duplicate costs
        # exactly what a success does up to this line, and there is no look-up-then-insert race.
        await self._users.add(user)
        login = Login.start(
            self._logins.next_identity(), user.id, refresh_token_hash, now, self._refresh_lifetime
        )
        await self._logins.add(login)
        access_token = self._tokens.issue(user.id, now)

        await self._events.publish(*user.release_events(), *login.release_events())
        return Authenticated(user=user, login=login, access_token=access_token)
