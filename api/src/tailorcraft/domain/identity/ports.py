"""Ports the `identity` context needs from the outside world, in the domain's own language.

`GuestSessionRepository` names no library, no HTTP detail, no cookie, no hashing algorithm — those
are `infrastructure/api/guest_session.py`'s business (ADR-0010). Implemented in
`infrastructure/persistence/repositories/identity/guest_session.py`; neither module is imported here.

Slice 2.1 adds five (technical plan §1): `UserRepository`, `LoginRepository`, `PasswordHasherPort`,
`AccessTokenPort` and `FailedLoginObserver` (the last added at T12, where `LogIn`'s need for it
became visible — its docstring says why). **None of them names an algorithm, a token format, a
transport or a cookie.** The domain knows that a password becomes a `PasswordHash`, that a refresh token is looked
up by its `TokenHash`, and that an access token turns back into a `UserId` or is refused with a
reason; which hash function, which signature scheme and which header carries what is
`infrastructure/identity/`'s business (ADR-0020, ADR-0021, AC-4, AC-7).

Slice 2.4 adds `GuestWorkClaimPort`: the hand-off of a guest session's work to a signed-in user
(ADR-0025 decision 8). Like the rest, it names no table, no SQL and no other context's aggregate.

Slice 2.5 adds five (technical plan §1, ADR-0026 … ADR-0028): `PendingRegistrationRepository`,
`PasswordResetRepository`, `OneTimeTokenPort`, `AccountMailPort` and `AccountMailQueuePort`; and it
widens `UserRepository` (`get_for_update`, `confirm_credential_unchanged`) and `LoginRepository`
(`remove_all_for_user`). None names SMTP, a vendor, a hash function, a queue technology or a lock
mode — the lock **order** is named, because callers must keep it; how a lock is taken is the
adapter's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from tailorcraft.domain.identity.account_mail import AccountMail
from tailorcraft.domain.identity.claim import ClaimedGuestWork
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    IssuedAccessToken,
    LoginId,
    MintedOneTimeToken,
    Password,
    PasswordHash,
    PasswordResetId,
    PasswordVerdict,
    PendingRegistrationId,
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
        """Persist a change to an existing user — the rehash-on-login path (I-11) and, since slice
        2.5, a password reset (`User.reset_password`, ADR-0028)."""
        ...

    async def get_for_update(self, user_id: UserId) -> User:
        """`get`, with the user row locked exclusively until the end of the current transaction
        (technical plan §0.7, §0.8). Raises `UserNotFound` if no user has this id.

        `ResetPassword` takes this **first**, before it re-finds and locks the reset row: everything
        that takes both a user and a reset takes the **user first** (§0.8's lock order), so a reset
        and an account erasure — which also locks the user first — queue on one row instead of
        deadlocking on two. It is also what makes a login racing a reset safe from the reset's side:
        a login holding `confirm_credential_unchanged`'s shared lock makes this wait until that login
        has committed, and the reset then deletes the login it created.
        """
        ...

    async def confirm_credential_unchanged(self, user_id: UserId, seen: PasswordHash) -> bool:
        """Whether `user_id`'s stored hash is still exactly `seen` — the hash the caller just verified
        a password against — taking a **shared** lock on the user row that is held until the end of
        the current transaction (technical plan §0.7, ADR-0028).

        `LogIn` and `DeleteOwnAccount` call it after a matching verify and **before any write**.
        Verifying costs ~50 ms off the loop, and a reset can commit inside that window:

        - **A reset committed first** → the stored hash is no longer `seen` → `False`, and the
          caller answers exactly as it does for a wrong password. The old password must not open a
          `Login` that would outlive the reset that was meant to evict it.
        - **This lock is taken first** → the reset's exclusive lock (`get_for_update`) waits until
          the caller commits, and the reset then deletes whatever `Login` it wrote.

        **Compared on the hash, not on `password_updated_at`**: timestamps are whole-second, so a
        reset in the same second as the previous change would compare equal; two hashes of even the
        same password differ by their salts. `False` also when the user row is gone.

        Not taken on the unknown-email or wrong-password paths, so 2.1's timing equality between
        them is untouched (AC-28).
        """
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

    async def remove_all_for_user(self, user_id: UserId) -> int:
        """Delete every login of `user_id` and everything each one issued — a password reset
        revokes every device (ADR-0028; ADR-0020: revocation is deletion). Returns how many logins
        went, which `User.reset_password` records as `logins_revoked`. Idempotent: 0 when there are
        none. Another user's logins are never touched.

        Called inside `ResetPassword`'s transaction, after `UserRepository.get_for_update` — so a
        login that was racing the reset has either committed (and is deleted here) or will find the
        credential changed (`confirm_credential_unchanged`).
        """
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


class FailedLoginObserver(Protocol):
    """Told *why* a login was refused, so the log line can say so while the error cannot (I-9, I-10).

    **Why this port exists at all.** `LogIn` must raise one `InvalidCredentials` with no attributes
    for both an unknown email and a wrong password (AC-9) — so nothing downstream can tell them apart
    by inspecting the error, and the response is byte-identical (AC-28). But the failure contract
    wants `identity.login_failed` with `reason=unknown_email`, or `reason=wrong_password` *and*
    `user_id`, and only the use case knows which it was. The application layer does not log (the
    house rule: a logging channel opened in `application/` is one the privacy tests cannot see), and
    1.6's answer — *return* the failure for the entry point to log — is unavailable because the
    outcome here is an exception. So the use case reports the cause through this port, immediately
    before raising, and the adapter in `infrastructure/` emits the line.

    **Two methods rather than one taking `(reason, user_id | None)`**: an unknown email has no user
    id, and a wrong password always has one, so the pairing is a rule the signature can hold instead
    of a docstring. Neither method takes the email, the password or any hash of either — the only
    fact that crosses is an id that already belongs to a real account.

    Synchronous, for `AccessTokenPort`'s reason: an adapter that emits one log line has nothing to
    await. Must not raise; a failure to *record* a refused login must never turn a 401 into a 500.
    """

    def unknown_email(self) -> None:
        """No account has the (normalized) email that was presented. The decoy verify has run."""
        ...

    def wrong_password(self, user_id: UserId) -> None:
        """The account `user_id` exists and the password presented does not match its hash."""
        ...


# --------------------------------------------------------------------------------------------------
# Slice 2.4 — a guest's work becomes a user's by an explicit claim (ADR-0025).
# --------------------------------------------------------------------------------------------------


class GuestWorkClaimPort(Protocol):
    """Moves every row a guest session owns to a user, in one transaction (ADR-0025 decision 8).

    **Why a port and not an aggregate.** The claim's invariant — *every row of a session changes
    owner together* — spans four contexts' tables (`intake`, `posting`, `tailoring`, `export`), and
    nothing in this process can hold it. One transaction in one adapter does, as ADR-0018 argued for
    the purge. **Nothing in any signature here names another context's aggregate**: a session going
    in, counts and `FileRef`s (inside `ClaimedGuestWork`) coming out. The adapter may import the
    four tables; the domain may not.

    **The order of the two methods is the lock order** (technical plan §0.6), and a caller calls them
    in this order or not at all:

    1. `lock_session` takes the guest session row `FOR UPDATE`.
    2. `transfer` takes the user row `FOR KEY SHARE`, implicitly, through each re-key's `user_id`
       foreign-key check.

    Every other actor takes at most one of the two rows, or both in this same order — the purge and
    a second claim meet the claim at (1), a guest write's FK check waits behind (1), and account
    erasure's `FOR UPDATE` on the user meets it at (2) — so there is no cycle to deadlock on.
    """

    async def lock_session(self, token_hash: str) -> GuestSession | None:
        """The guest session whose cookie hashes to `token_hash`, locked `FOR UPDATE` until the end of
        the transaction `transfer` commits — or `None` when there is no such row.

        `None` is ordinary, not exceptional: no cookie's session, one the purge already deleted, or
        one a concurrent claim already took (it waited on the lock, then found the row gone). The
        caller answers it with a report of zeros. **The lock is taken whether or not the session has
        expired**; judging that is `GuestSession.is_expired`'s job, applied by the caller.
        """
        ...

    async def transfer(self, session_id: GuestSessionId, user_id: UserId) -> ClaimedGuestWork:
        """Re-key everything `session_id` owns to `user_id`, drop its working copies, delete the
        session row, and report what moved.

        **Rows only, durable on return.** The bound adapter commits before returning, so a returned
        `ClaimedGuestWork` describes committed fact, and the session row it deleted is the purge's
        signal that a claim came first (§0.5). **No file is touched here**: the dropped working
        copies' keys come back in `files_to_unlink`, and unlinking them is the caller's next step —
        rows committed, then files (ADR-0006 amendment).

        Must be called after `lock_session` returned this session, in the same transaction. Raises
        `UserNotFound` when the user's foreign key refuses the re-key — the account was erased first
        — and nothing is committed.
        """
        ...


# --------------------------------------------------------------------------------------------------
# Slice 2.5 — email verification and password reset (ADR-0026, ADR-0027, ADR-0028).
#
# Two rules run through every port below, and each docstring restates the one it carries:
#
# - **No lookup at request time** (technical plan §0.2). Registering and asking for a reset each
#   write one row and enqueue one id, the same statements for every address; nothing on the request
#   path asks whether an account exists. The worker — which nobody can time — decides what is sent.
# - **The token is minted in the worker; commit, then send** (§0.4). The broker carries an id, never
#   a token. The delivery use case mints, stores the hash on the aggregate, commits, and only then
#   hands the plaintext to `AccountMailPort.send` — so every link that is mailed works, and a crash
#   between the two leaves no mail (recoverable by *Send it again*) rather than a dead link.
# --------------------------------------------------------------------------------------------------


class PendingRegistrationRepository(Protocol):
    """Persistence for the `PendingRegistration` aggregate (ADR-0027).

    **`put` is the supersede** (technical plan §0.3), as `UserRepository.add` is the uniqueness
    check: "one pending registration per address, newest wins" is a property of the set, which only
    the unique index sees atomically. There is no `find_by_email` here, and that absence is §0.2's
    rule — the request path must not be able to ask.
    """

    def next_identity(self) -> PendingRegistrationId:
        """Mint an id for a pending registration that does not exist yet. Synchronous: application-
        assigned UUIDv7 needs no I/O (ADR-0007)."""
        ...

    async def put(self, pending: PendingRegistration) -> None:
        """Store `pending` as **the** pending registration for its address, replacing any other —
        new id, new password hash, no token — in one statement, durable on return (§0.3).

        No read first: the request path is the same statement whether or not a row existed, so it
        neither branches nor races a concurrent registration for the same address (AC-17). The
        replaced row's id disappears, so a delivery task still queued for it finds nothing and sends
        nothing; the replaced row's link, if one was mailed, stops working.
        """
        ...

    async def get(self, pending_id: PendingRegistrationId) -> PendingRegistration | None:
        """The pending registration with this id, or `None` — the ordinary answer for a delivery task
        whose row was superseded, confirmed or swept before the worker reached it."""
        ...

    async def lock_by_token_hash(self, token_hash: TokenHash) -> PendingRegistration | None:
        """The pending registration whose issued token hashes to `token_hash`, locked exclusively
        until the end of the current transaction, or `None` (unknown, superseded, already used or
        swept — one answer, so a guesser learns nothing).

        `ConfirmRegistration`'s first step (§0.8: pending row, then the `User` insert, then the
        delete). Two clicks of one link queue here; the second finds nothing.
        """
        ...

    async def save_issued(self, pending: PendingRegistration) -> None:
        """Persist the token hash and `issued_at` that `PendingRegistration.issue` set, durable on
        return — this is the *commit* in §0.4's *commit, then send*.

        Writes only over a row that is **not yet issued**. Raises `PendingRegistrationAlreadyIssued`
        when the stored row was issued meanwhile (a concurrent delivery of the same id won), so a
        redelivered task sends no second mail (AC-39).
        """
        ...

    async def remove(self, pending_id: PendingRegistrationId) -> None:
        """Delete the pending registration. **Idempotent**: one already gone is success."""
        ...


class PasswordResetRepository(Protocol):
    """Persistence for the `PasswordReset` aggregate (ADR-0028).

    `add`, not `put`: a reset request does **not** supersede at request time. The request path knows
    only an address and may not look anything up (§0.2), so it cannot know which account's resets to
    replace; that happens at delivery, when the worker has found the account (`save_issued`).
    """

    def next_identity(self) -> PasswordResetId:
        """Mint an id for a reset that does not exist yet. Synchronous, as every `next_identity`."""
        ...

    async def add(self, reset: PasswordReset) -> None:
        """Insert a newly requested (addressed) reset — the one write of the request path, durable
        on return: the enqueue that follows must never hand the worker an id it cannot yet see."""
        ...

    async def get(self, reset_id: PasswordResetId) -> PasswordReset | None:
        """The reset with this id, or `None` (superseded, used or swept before the worker came)."""
        ...

    async def find_by_token_hash(self, token_hash: TokenHash) -> PasswordReset | None:
        """The issued reset whose token hashes to `token_hash`, **without** a lock, or `None`.

        `ResetPassword`'s first read, taken unlocked on purpose: it yields the `user_id` whose row
        must be locked **first** (§0.8). The reset is then re-found and locked with
        `lock_by_token_hash`, which is the read the use case acts on.
        """
        ...

    async def lock_by_token_hash(self, token_hash: TokenHash) -> PasswordReset | None:
        """`find_by_token_hash`, locked exclusively until the end of the current transaction, or
        `None` if it went between the two reads (used by a concurrent confirm, superseded, swept).

        Must be called **after** `UserRepository.get_for_update` on the reset's user (§0.8).
        """
        ...

    async def save_issued(self, reset: PasswordReset) -> None:
        """Persist the issue `PasswordReset.issue` made — the account it is now for, its token hash,
        `issued_at`, and the address cleared — **and delete every other reset of that account**, in
        one unit, durable on return (§0.4's *commit*; one live reset link per account).

        Writes only over a row that is not yet issued. Raises `PasswordResetAlreadyIssued` when the
        stored row was issued meanwhile (a concurrent delivery won), so a redelivery sends nothing.
        """
        ...

    async def remove(self, reset_id: PasswordResetId) -> None:
        """Delete one reset. **Idempotent**: one already gone is success."""
        ...

    async def remove_all_for_user(self, user_id: UserId) -> int:
        """Delete every reset issued to `user_id`, returning how many — a successful reset spends its
        own link and any other (ADR-0028). Idempotent: 0 when there are none."""
        ...


class OneTimeTokenPort(Protocol):
    """Mints the one-time token a confirmation or reset link carries, with the hash stored in its
    place (technical plan §0.4).

    **Only the worker's delivery use cases call this.** If the request path minted, the plaintext
    would ride in the broker until a worker took it — a credential in a store kept for something
    else. Minting in the worker keeps it in one process's memory and in one mail. A token **presented**
    by a browser never comes through here: the route hashes it (infrastructure, as the refresh cookie
    is hashed), so the confirm and reset use cases receive a `TokenHash` and never a plaintext.

    **Synchronous**, for `AccessTokenPort`'s reason: random bytes and one hash are microseconds, with
    no I/O, and a thread hop would cost more than the work.
    """

    def mint(self) -> MintedOneTimeToken:
        """A new random token and its hash. Never returns the same token twice in practice."""
        ...


class AccountMailPort(Protocol):
    """Sends the mail an account is sent: a confirmation link, an account-exists notice, a reset link
    (ADR-0026). Which message and what it carries is the domain's (`AccountMail`); subject, wording,
    link URL and transport are the adapter's.

    **This is where the plaintext token leaves `application/`** — the one place it crosses that
    layer at all (§0.4). `send` is called **after** the token's hash is committed, never before: a
    crash between the two then leaves no mail rather than a link to nothing. The adapter reads the
    token with `OneTimeToken.reveal()` to write the link, and nowhere else.

    **Retry is inside**, bounded, for transient failures only; the caller never retries `send`, and
    the queue that ran it does not either. **Every failure is `MailNotDelivered`** — specific
    translations on top, an `except Exception` floor underneath, so the promise holds by
    construction. **The adapter never logs the recipient, the token or the message body**, and a
    `MailNotDelivered` carries a reason and at most a numeric reply code, never the server's text.
    """

    async def send(self, mail: AccountMail) -> None:
        """Deliver `mail` to the mail provider. Raises `MailNotDelivered(reason, smtp_code)`."""
        ...


class AccountMailQueuePort(Protocol):
    """Hands a pending registration or a reset to the worker that will decide and send its mail.

    **Carries an id and nothing else** (§0.4): no address, no token, no password hash. The worker
    re-reads the row by id, so a row superseded or removed in the meantime is simply not found.
    Called **after** the row is committed: an id enqueued for an uncommitted row would be a task
    that can find nothing.

    Both methods are the request path's last step, the same for every address (§0.2). Each raises
    `AccountMailQueueUnavailable` when the broker cannot take it; the row is already committed, and
    the user's recovery is *Send it again*.
    """

    async def enqueue_registration(self, pending_id: PendingRegistrationId) -> None: ...

    async def enqueue_password_reset(self, reset_id: PasswordResetId) -> None: ...
