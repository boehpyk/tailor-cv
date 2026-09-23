"""The `Login` aggregate: one refresh-token family — one device's continuing sign-in (ADR-0020).

**Invariant, and it is arithmetic:** a login has exactly one current token hash; each rotation
retires the current one at the current generation and advances the generation by exactly one; an
expired login never rotates. That makes "was this token stolen?" a comparison of two integers and two
instants — unit-testable with no I/O (AC-6) — rather than a query over a table of tokens.

**Revocation is deletion** (ADR-0020). There is no `revoked` state here and no method that sets one:
a revoked login is a row that no longer exists, after which every token it ever issued is simply
unknown. The aggregate *records* that a logout or a reuse happened; `LoginRepository.remove` does the
deleting.

**`expires_at` is absolute** (OQ-9): rotation never extends it. A login lives its lifetime from the
password entry that created it, however often it is refreshed, so a stolen-and-refreshed token cannot
keep itself alive for ever.

**`version` is the repository's, not the aggregate's** — `start` sets it to 1 and nothing here moves
it. `LoginRepository.save_rotation` issues `UPDATE … WHERE version = :loaded` and bumps it, which is
how two concurrent rotations of one current token become one success and one
`LoginConcurrentlyRotated` (I-25) — the same division of labour ADR-0015 chose for `TailoringRun`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tailorcraft.domain.identity.value_objects import (
    LoginId,
    RetiredRefreshToken,
    RetiredTokenVerdict,
    TokenHash,
    UserId,
)
from tailorcraft.domain.shared.events import RecordsEvents

# How long after a rotation the immediate predecessor still counts as a lost race rather than a
# replay (technical plan §0.3, AC-6). A **domain constant, not a setting**: it is a property of what a
# "race" between two tabs is, and the codebase's rule is that a control does not get a knob
# (ADR-0012's no-off-switch). Inclusive: `at - rotated_at == REFRESH_RACE_GRACE` is still `RACED`.
REFRESH_RACE_GRACE = timedelta(seconds=10)


# NOT `slots=True`: mapped imperatively, like `User` and `GuestSession` — see `user.py` for the
# reason. The private-attribute names below are exactly what
# `infrastructure/persistence/mapping/identity/login.py` will target (ADR-0007).
class Login(RecordsEvents):
    """One refresh-token family.

    State: `_id`, `_user_id`, `_created_at`, `_expires_at`, `_generation`, `_current_token_hash`,
    `_rotated_at` (`None` until the first rotation), `_version` — private, exposed only through
    read-only properties.
    """

    # Class-level annotations only: `__init__` sets nothing (see `User` for why this is how
    # `mypy --strict` learns the attribute types). `_recorded_events` is not mapped.
    _id: LoginId
    _user_id: UserId
    _created_at: datetime
    _expires_at: datetime
    _generation: int
    _current_token_hash: TokenHash
    _rotated_at: datetime | None
    _version: int

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build a `Login` with `start`.

        Must stay, and must not raise — `User.__init__` gives the reason: without it the mapper
        installs a constructor accepting mapped attribute names, and `Login(_generation=5)` would be a
        family that skipped four rotations it never had.
        """

    @classmethod
    def start(
        cls,
        id: LoginId,
        user_id: UserId,
        token_hash: TokenHash,
        at: datetime,
        lifetime: timedelta,
    ) -> Login:
        """The only way to create a `Login`: generation 1, `token_hash` current, `created_at = at`,
        `expires_at = at + lifetime`, `rotated_at = None`, `version = 1`.

        Records `LoggedIn(user_id, login_id, occurred_at=at)`. Raises `InvariantViolated` if
        `lifetime <= 0` — `expires_at` must be after `created_at`, and this does not trust the caller
        (the settings object) to have enforced it.
        """
        raise NotImplementedError

    def is_expired(self, at: datetime) -> bool:
        """Whether `at` is at or past `expires_at` — inclusive, matching `GuestSession.is_expired`."""
        raise NotImplementedError

    def rotate(self, new_hash: TokenHash, at: datetime) -> RetiredRefreshToken:
        """Retire the current token and install `new_hash` as current.

        Returns `RetiredRefreshToken(old_hash, old_generation, retired_at=at)` for the repository to
        persist; sets `generation += 1` and `rotated_at = at`. Leaves `expires_at` unchanged
        (absolute lifetime) and `version` unchanged (the repository's). Raises `LoginExpired` if
        `is_expired(at)` — an expired family never rotates, whatever token it is shown.

        Records no event: one every 15 minutes per tab is a log flood with no listener.
        """
        raise NotImplementedError

    def judge_retired(self, generation: int, at: datetime) -> RetiredTokenVerdict:
        """Decide what a retired token of `generation`, presented at `at`, means (AC-6).

        `RACED` iff `generation == self.generation - 1` **and** the login has rotated **and**
        `at - rotated_at <= REFRESH_RACE_GRACE` — a second tab that lost a race by a few seconds.
        Otherwise `REUSED`, recording `RefreshTokenReuseDetected(user_id, login_id,
        generation_presented=generation, generation_current=self.generation, occurred_at=at)`: a
        generation two behind is `REUSED` even at 0 s. Raises `LoginExpired` if `is_expired(at)`.

        Changes no state either way: `RACED` must leave everything as it was (I-23), and on `REUSED`
        the revocation is the repository's deletion, not a flag here.
        """
        raise NotImplementedError

    def record_logout(self, at: datetime) -> None:
        """Record `LoggedOut(user_id, login_id, occurred_at=at)`. The deletion is the repository's;
        the aggregate records the fact so the use case can publish it after the delete commits."""
        raise NotImplementedError

    @property
    def id(self) -> LoginId:
        raise NotImplementedError

    @property
    def user_id(self) -> UserId:
        raise NotImplementedError

    @property
    def created_at(self) -> datetime:
        raise NotImplementedError

    @property
    def expires_at(self) -> datetime:
        raise NotImplementedError

    @property
    def generation(self) -> int:
        raise NotImplementedError

    @property
    def current_token_hash(self) -> TokenHash:
        raise NotImplementedError

    @property
    def rotated_at(self) -> datetime | None:
        raise NotImplementedError

    @property
    def version(self) -> int:
        raise NotImplementedError
