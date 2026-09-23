"""Ports the `identity` context needs from the outside world, in the domain's own language.

`GuestSessionRepository` names no library, no HTTP detail, no cookie, no hashing algorithm — those
are `infrastructure/api/guest_session.py`'s business (ADR-0010). Implemented in
`infrastructure/persistence/repositories/identity/guest_session.py`; neither module is imported here.

Slice 2.1 adds four (technical plan §1): `UserRepository`, `LoginRepository`, `PasswordHasherPort`
and `AccessTokenPort`. **None of them names an algorithm, a token format, a transport or a
cookie.** The domain knows that a password becomes a `PasswordHash`, that a refresh token is looked
up by its `TokenHash`, and that an access token turns back into a `UserId` or is refused with a
reason; which hash function, which signature scheme and which header carries what is
`infrastructure/identity/`'s business (ADR-0020, ADR-0021, AC-4, AC-7).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    IssuedAccessToken,
    LoginId,
    Password,
    PasswordHash,
    PasswordVerdict,
    RetiredRefreshToken,
    TokenHash,
    UserId,
)


class GuestSessionRepository(Protocol):
    """Persistence for the `GuestSession` aggregate."""

    def next_identity(self) -> GuestSessionId:
        """Mint an id for a `GuestSession` that does not exist yet. Synchronous for the same reason
        `BaseCvRepository.next_identity` is (`domain/intake/ports.py`): application-assigned UUIDv7
        needs no I/O."""
        ...

    async def add(self, session: GuestSession) -> None: ...

    async def get(self, session_id: GuestSessionId) -> GuestSession:
        """Raises `GuestSessionNotFound` if no `GuestSession` with this id exists.

        `get`, not `find` — used by `UploadBaseCv` (technical-plan.md step 1), which is handed a
        `GuestSessionId` it already resolved from a valid cookie in this same request; if the row is
        gone by the time the use case runs, that is exceptional (the session expired *and* was
        purged, or the id is stale), not an ordinary branch the use case is expected to handle. See
        `find_by_token_hash` below for the "absence is ordinary" counterpart.
        """
        ...

    async def find_by_token_hash(self, token_hash: str) -> GuestSession | None:
        """Look up a session by the hash of whatever cookie arrived on the request. Returns `None`
        rather than raising when there is no match, because "no such session" — a missing cookie, an
        expired one already purged, a forged value — is the ordinary shape of an anonymous or expired
        visitor and every caller must handle it, not the exceptional case `get` above models."""
        ...


# --------------------------------------------------------------------------------------------------
# Slice 2.1 — registered users and their logins.
# --------------------------------------------------------------------------------------------------


class UserRepository(Protocol):
    """Persistence for the `User` aggregate.

    **`add` is the uniqueness check** (technical plan §0.4, I-5, I-6). "No two users share an email"
    is a property of the set of users, which only the unique index sees atomically; there is
    deliberately no `exists_by_email` here, because a look-up-then-insert is a race the index is
    not, and a method that invites it would be used.
    """

    def next_identity(self) -> UserId:
        """Mint an id for a `User` that does not exist yet. Synchronous: application-assigned
        UUIDv7 needs no I/O (ADR-0007)."""
        ...

    async def add(self, user: User) -> None:
        """Insert a new user. Raises `EmailAlreadyRegistered` when the normalized email is already
        taken — translated from the unique-index violation, never pre-checked with a `SELECT`."""
        ...

    async def get(self, user_id: UserId) -> User:
        """Raises `UserNotFound` if no user has this id — a valid access token whose user row is gone
        (I-39). `get`, not `find`: the caller holds an id it has reason to believe exists."""
        ...

    async def find_by_email(self, email: EmailAddress) -> User | None:
        """The login look-up. `None` is the ordinary "no such account" branch, which `LogIn` must
        still answer at the cost of a real verify (`PasswordHasherPort.verify`'s docstring)."""
        ...

    async def save(self, user: User) -> None:
        """Persist a change to an existing user — today only the rehash-on-login path (I-11)."""
        ...


class LoginRepository(Protocol):
    """Persistence for the `Login` aggregate and its append-only index of retired tokens.

    **Revocation is deletion** (ADR-0020): `remove` is how a login ends, whether by logout, by a
    detected reuse or by the break-glass. There is no `save` for an arbitrary change, because the
    only change a live login undergoes is a rotation, and a rotation has its own concurrency rule.
    """

    def next_identity(self) -> LoginId:
        """Mint an id for a `Login` that does not exist yet. Synchronous, as `UserRepository`'s."""
        ...

    async def add(self, login: Login) -> None: ...

    async def find_by_current_token_hash(self, token_hash: TokenHash) -> Login | None:
        """The login whose *current* refresh token hashes to `token_hash`, or `None` — the ordinary
        shape of an unknown, forged, revoked or already-rotated token (the retired look-up below
        tells the last of those apart)."""
        ...

    async def find_by_retired_token_hash(self, token_hash: TokenHash) -> tuple[Login, int] | None:
        """The login that once issued `token_hash`, and the generation it was issued at, or `None`.

        The generation is what `Login.judge_retired` needs to tell a lost race (the immediate
        predecessor, within the grace) from a replay (anything older) — reuse is detectable at
        *any* generation, not only the last one (ADR-0020's option (c)). Returns a pair rather than
        a `RetiredRefreshToken` because the caller already holds the hash and needs only the number.
        """
        ...

    async def save_rotation(self, login: Login, retired: RetiredRefreshToken) -> None:
        """Persist a rotation: the login's new current hash, generation and `rotated_at`, and the
        `retired` token into the retired index — as one unit.

        **Optimistic concurrency is this method's job, not the aggregate's** (`Login` never moves
        its own `version`). The write succeeds only if the stored `version` is still the one the
        login was loaded with, and bumps it. Raises `LoginConcurrentlyRotated` if the version has
        moved or the retired token is already recorded — the loser of two concurrent rotations of
        one current token (I-25). The caller translates that to `RefreshInProgress`, never to a
        revocation.
        """
        ...

    async def remove(self, login_id: LoginId) -> None:
        """Delete the login and everything it ever issued. **Idempotent**: removing a login that is
        already gone is success, not `LoginNotFound` — a logout retried, or racing a reuse
        revocation, must not fail for having arrived second (AC-11)."""
        ...

    async def count_all(self) -> int:
        """How many logins exist. What `RevokeAllLogins(dry_run=True)` reports, so the break-glass
        can be rehearsed without being pulled (AC-13, OQ-5)."""
        ...

    async def remove_all(self) -> int:
        """Delete every login — the break-glass that signs everybody out (AC-13, OQ-5). Returns how
        many were removed. Idempotent: on an empty table it returns 0."""
        ...


class PasswordHasherPort(Protocol):
    """Turns a `Password` into a `PasswordHash`, and checks one against the other.

    **Async because it is expensive by design** — a password hash is tuned to cost tens of
    milliseconds of CPU and memory, which on the event loop would stall every concurrent request
    (ADR-0021). The adapter runs it off the loop; this port only says that awaiting it is how it is
    used. Every failure of the underlying library is `PasswordHashingFailed`.
    """

    async def hash(self, password: Password) -> PasswordHash:
        """Hash `password` under today's parameters. Raises `PasswordHashingFailed`."""
        ...

    async def verify(self, password: Password, against: PasswordHash | None) -> PasswordVerdict:
        """Compare `password` with the stored hash `against`.

        `MATCH_NEEDS_REHASH` means a match whose hash was made under older parameters; the caller
        replaces it in the same unit of work (I-11).

        **`against=None` means "there is no such account", and it is a decoy, not an optimisation
        opportunity.** The implementation must verify `password` against a decoy hash at the same
        cost as a real verify and return `MISMATCH`. Returning early would make an unknown email
        measurably faster than a wrong password, and that timing difference answers "is this email
        registered?" for anybody with a stopwatch — the enumeration AC-9 and AC-28 exist to close.
        A reviewer who sees a `None` check at the top of an implementation that returns without
        hashing is looking at the bug.

        Raises `PasswordHashingFailed`.
        """
        ...


class AccessTokenPort(Protocol):
    """Issues the short-lived bearer credential that says "this request is from `user_id`", and
    turns one back into a `UserId` or refuses it.

    **Synchronous on purpose**, unlike `PasswordHasherPort`. Signing or checking a token is a keyed
    hash over a few hundred bytes — microseconds — and a thread hop would cost more than the work.
    The contrast is the point: offload by *measured* cost, not by "it's crypto". If an
    implementation ever needs I/O (a key fetched from a remote store, a revocation list), that is a
    different port, not a reason to make this one async.

    Both methods take the instant from the caller (the `Clock` port, via the use case or the
    dependency) so a test can pin expiry without the adapter reading the wall clock.
    """

    def issue(self, user_id: UserId, at: datetime) -> IssuedAccessToken:
        """Mint a token for `user_id`, issued at `at`, with its relative lifetime."""
        ...

    def verify(self, token: str, at: datetime) -> UserId:
        """Return the user the token speaks for, as of `at`. Raises `AccessTokenInvalid(reason)`
        for every refusal (I-33 … I-38); the reason is for the log line, never for the client."""
        ...
