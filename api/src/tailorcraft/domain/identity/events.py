"""Domain events for the `identity` bounded context (slice 2.1).

**Ids, integers and instants only — never an email (AC-5).** `LoggingEventPublisher`
(`infrastructure/events/logging_publisher.py`) logs **every field of every event it receives**, so an
event's field set *is* a log field set: an email in a payload is an email in a log, and a hash or a
token in a payload is half a credential in one. AC-5 pins each of the six field sets below exactly
rather than trusting this docstring (five from 2.1; slice 2.5 adds `PasswordChangedByReset`).

**No rotation event.** A refresh rotates every 15 minutes per open tab; an event for each would be a
log flood with no listener. Reuse is the rotation fact anybody needs, and it has its own event.

`GuestSession` records no events and gains none here: slice 1.1 did not give it any, and `User` and
`GuestSession` share nothing on purpose (ADR-0008, ADR-0010).

These are pure data with no behaviour — the dataclass field list *is* the signature — so, as in every
other context, this module is written whole at the skeleton stage.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.value_objects import LoginId, Role, UserId
from tailorcraft.domain.shared.events import DomainEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class UserRegistered(DomainEvent):
    """A person created an account. Payload: `user_id` (+ `occurred_at`). **Not the email** — the
    obvious field to add, and the one AC-5 exists to keep out."""

    user_id: UserId


@dataclass(frozen=True, slots=True, kw_only=True)
class UserPasswordRehashed(DomainEvent):
    """A user's stored hash was replaced with one made under today's parameters, on a successful
    login (I-11, ADR-0021). Payload: `user_id` (+ `occurred_at`). Neither hash."""

    user_id: UserId


@dataclass(frozen=True, slots=True, kw_only=True)
class LoggedIn(DomainEvent):
    """A login — one refresh-token family, one device — began. Payload: `user_id`, `login_id`
    (+ `occurred_at`). Not the token hash."""

    user_id: UserId
    login_id: LoginId


@dataclass(frozen=True, slots=True, kw_only=True)
class LoggedOut(DomainEvent):
    """A login was ended by its holder (I-29). Payload: `user_id`, `login_id` (+ `occurred_at`).
    Recorded by the aggregate; the deletion itself is the repository's."""

    user_id: UserId
    login_id: LoginId


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshTokenReuseDetected(DomainEvent):
    """A retired refresh token came back outside the race grace, or two or more generations old
    (I-24): somebody holds a token they should not, and the login is being revoked.

    Payload: `user_id`, `login_id`, `generation_presented`, `generation_current` (+ `occurred_at`).
    The two generations are what make the report answerable later — "one behind, 40 s late" reads as
    a lost response (I-26); "twelve behind" reads as theft. **Never either hash.**
    """

    user_id: UserId
    login_id: LoginId
    generation_presented: int
    generation_current: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PasswordChangedByReset(DomainEvent):
    """A user proved their address with a reset link and set a new password; every login they had
    was revoked in the same transaction (ADR-0028). Payload: `user_id`, `logins_revoked`
    (+ `occurred_at`).

    `logins_revoked` is what makes the event worth reading — "0" is a forgotten password, "4" is
    somebody evicting the devices they suspect. **Neither hash, no token, no address** — the event
    that follows a credential change is the most tempting place to put the credential.

    A separate type from `UserPasswordRehashed` on purpose: that one is a parameter upgrade on a
    successful login (same password, new hash); this one is a *different password*. One event with a
    `cause` field would let a listener that only wanted one of them act on both.
    """

    user_id: UserId
    logins_revoked: int


@dataclass(frozen=True, slots=True, kw_only=True)
class UserRoleChanged(DomainEvent):
    """A user's role changed (slice 4.1, ADR-0032). Payload: `user_id`, `from_role`, `to_role`
    (+ `occurred_at`).

    The most security-relevant write in the system, so it is an event even with no listener:
    `LoggingEventPublisher` logs every field, which makes this the structured record of who became
    (or stopped being) an administrator — ids and role names only. Recorded only when the role
    actually changed; a no-op `change_role` records nothing.
    """

    user_id: UserId
    from_role: Role
    to_role: Role
