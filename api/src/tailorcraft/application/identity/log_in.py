"""The `LogIn` use case: check a password and start a new `Login` for it (AC-9, I-7 … I-11).

**SKELETON step (T12).** `__init__` is fully written and stores its arguments; `__call__`'s body is
deferred to T14.

**The design choice this module makes: the failure's cause leaves through a port, not through the
error.** Two requirements pull in opposite directions:

- AC-9: an unknown email and a wrong password raise **the same `InvalidCredentials`, with the same
  (empty) attributes**, so nothing downstream — the router, a log formatter, Sentry, a test — can
  tell them apart by inspecting the error, and the response is byte-identical (AC-28).
- I-9 / I-10: the log line says `reason=unknown_email`, or `reason=wrong_password` **with `user_id`**.

Only this use case knows which it was, and this layer does not log. 1.6's answer to "the application
knows something the log line needs" was to *return* it (`PurgeReport`'s failure tuples), but here the
outcome is an exception, and an exception carrying the cause is exactly what AC-9 forbids. So
`FailedLoginObserver` (`domain/identity/ports.py`) is called once, immediately before the raise:
`unknown_email()` or `wrong_password(user_id)`. Its adapter in `infrastructure/` emits
`identity.login_failed`, inside the privacy tests' field of view; the router catches
`InvalidCredentials` and logs nothing of its own about the cause.

Rejected on the way: a `__cause__`/`__context__` on the raised error (the router would have to read
it, which *is* distinguishing the two by inspecting the error); a `LoginFailed` domain event (AC-5
pins the event set at five, and an event is a fact about an aggregate, while an unknown email has
none); and an `Authenticated | LoginRefused` return (AC-9 and T13 pin the raise).

**Timing.** The observer is called on both paths after the verify, so it adds the same cost to each;
it must not do anything on one path that it skips on the other.
"""

from __future__ import annotations

from datetime import timedelta

from tailorcraft.application.identity.results import Authenticated
from tailorcraft.domain.identity.ports import (
    AccessTokenPort,
    FailedLoginObserver,
    LoginRepository,
    PasswordHasherPort,
    UserRepository,
)
from tailorcraft.domain.identity.value_objects import TokenHash
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


class LogIn:
    """Verify `raw_password` for `raw_email` and start a `Login` (AC-9).

    Constructor arguments: the ports `users`, `logins`, `hasher`, `tokens`, `clock`, `events`,
    `failed_logins` (see the module docstring), and `refresh_lifetime` — the new `Login`'s absolute
    lifetime, as in `RegisterUser`. **No `PasswordPolicy`**: a login checks the password against the
    stored hash and nothing else, so a policy tightened next year cannot lock out anybody who
    registered under this one (`PasswordPolicy`'s docstring).

    Flow of `__call__` (technical plan §2), with **one `clock.now()` for the whole call**:

    1. `EmailAddress.parse(raw_email)`, `Password.from_input(raw_password)` — either may raise
       (`InvalidEmailAddress`, `WeakPassword` for empty or over the input bound).
    2. `user = await users.find_by_email(email)`.
    3. `verdict = await hasher.verify(password, user.password_hash if user else None)` — **exactly
       once, on every path**; `None` makes the adapter verify against a decoy at equal cost.
    4. `user is None` → `failed_logins.unknown_email()`, raise `InvalidCredentials()`.
       `MISMATCH` → `failed_logins.wrong_password(user.id)`, raise `InvalidCredentials()`.
       No `Login` is created and nothing is written on either.
    5. `MATCH_NEEDS_REHASH` → `user.replace_password_hash(await hasher.hash(password), now)`;
       `await users.save(user)` — in the same unit of work (I-11).
    6. `Login.start(logins.next_identity(), user.id, refresh_token_hash, now, refresh_lifetime)`;
       `await logins.add(login)`; `tokens.issue(user.id, now)`.
    7. `await events.publish(*user.release_events(), *login.release_events())` after the writes;
       return `Authenticated(user, login, access_token)`.
    """

    def __init__(
        self,
        users: UserRepository,
        logins: LoginRepository,
        hasher: PasswordHasherPort,
        tokens: AccessTokenPort,
        clock: Clock,
        events: EventPublisherPort,
        failed_logins: FailedLoginObserver,
        refresh_lifetime: timedelta,
    ) -> None:
        self._users = users
        self._logins = logins
        self._hasher = hasher
        self._tokens = tokens
        self._clock = clock
        self._events = events
        self._failed_logins = failed_logins
        self._refresh_lifetime = refresh_lifetime

    async def __call__(
        self,
        raw_email: str,
        raw_password: str,
        refresh_token_hash: TokenHash,
    ) -> Authenticated:
        raise NotImplementedError
