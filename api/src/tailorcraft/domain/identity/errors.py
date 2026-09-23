"""Errors the `identity` bounded context raises: about a guest session, and — from slice 2.1 — about
a user, a password and a login.

As in every other context: an error class *is* its contract, so every one of these gets a real body
even though the slice-2.1 half lands in a SKELETON step. An error the RED test has to *construct*
must be constructible, and one that carries data must actually store it, or the red fails on a
`TypeError` from the skeleton rather than on the assertion it was written for.

The domain never raises `HTTPException` and never carries a status code; the failure contract's rows
I-1 … I-47 in the feature spec say which status and which `code` each of these becomes.

**Nothing here carries an email, a password, a hash or a token** — not as an attribute and not in a
message. Identity is the context where that rule is easiest to break, because every one of these
errors is raised *about* exactly those values, and "include the thing that was wrong" is the reflex
of every error message ever written. The attributes below are reasons (closed enums) and policy
numbers; nothing a stranger typed.
"""

from __future__ import annotations

from tailorcraft.domain.identity.value_objects import (
    AccessTokenRefusal,
    InvalidEmailReason,
    WeakPasswordReason,
)
from tailorcraft.domain.shared.errors import DomainError


class GuestSessionNotFound(DomainError):
    """No `GuestSession` exists for the token hash presented — an unrecognized or forged cookie."""


class GuestSessionExpired(DomainError):
    """The session exists but `expires_at` has passed (ADR-0006 §1). A GET on a base CV refuses an
    expired session rather than minting a new one (F-19); a POST is more forgiving and starts a
    fresh session instead (F-17/F-18) — that asymmetry lives in the use cases, not here."""


# --------------------------------------------------------------------------------------------------
# Slice 2.1 — registration, login, refresh, access tokens.
# --------------------------------------------------------------------------------------------------


class InvalidEmailAddress(DomainError):
    """An email address broke a rule of AC-2 (I-1, I-12). Carries **which** rule, never the value.

    The API answers every reason with the one code `invalid_email`; the reason exists for the test
    table and the log line (`InvalidEmailReason` says why).
    """

    def __init__(self, reason: InvalidEmailReason) -> None:
        super().__init__(f"email address refused: {reason.value}")
        self.reason = reason


class WeakPassword(DomainError):
    """A password was refused — by `PasswordPolicy` at registration (I-2, I-3, I-4) or by
    `Password.from_input`'s input bounds on any endpoint.

    Carries the reason **and the bounds that were applied**, so the 422 envelope can say
    `min_length: 12` from the object that actually refused rather than from a second copy of the
    number in the router (technical plan §1: the policy is data). Never the password, and never its
    length — a length is a fact about a secret.
    """

    def __init__(self, reason: WeakPasswordReason, min_length: int, max_length: int) -> None:
        super().__init__(f"password refused: {reason.value}")
        self.reason = reason
        self.min_length = min_length
        self.max_length = max_length


class EmailAlreadyRegistered(DomainError):
    """The unique index on the normalized email refused a second `User` (I-5, I-6).

    Raised by `UserRepository.add`, **not** by `User` and not by a use case's `SELECT`: "no two users
    share an email" is a property of the *set* of users, which no single aggregate can see, and a
    look-up-then-insert is a race the index is not (technical plan §0.4). Carries nothing — the
    caller already knows the email, and the log line must not.
    """


class InvalidCredentials(DomainError):
    """Login refused: unknown email **or** wrong password (I-9, I-10).

    **No attributes, deliberately (AC-9).** The two causes raise this same type with the same (empty)
    state so that nothing downstream — the router, a log formatter, a test — can tell them apart by
    inspecting the error; the response must be byte-identical (AC-28). Which cause it was is known to
    the use case, which is where the `reason=` of the log line comes from.
    """


class UserNotFound(DomainError):
    """No `User` exists with the requested id — a valid access token whose user row is gone (I-39)."""


class LoginNotFound(DomainError):
    """The presented refresh token belongs to no live login: unknown, forged, already revoked, or the
    login just expired and was deleted on sight (I-20, I-21, I-27). All of them are the same
    `not_signed_in` to the client, because after a revocation "revoked" and "never existed" are
    indistinguishable by design (ADR-0020: revocation is deletion)."""


class LoginExpired(DomainError):
    """`Login.rotate` or `Login.judge_retired` was asked to act on a login at or past its absolute
    `expires_at`. The use case deletes the login and answers `LoginNotFound` (I-21) — this type never
    reaches the router; it is the aggregate saying *why* a rotation is refused."""


class RefreshInProgress(DomainError):
    """Another refresh of this login won a race this one lost (I-23, I-25): the immediate predecessor
    presented within the grace, or a concurrent rotation of the same current token. **Nothing
    changed**; the client waits and retries, by when its cookie is the winner's (technical plan
    §0.3). Never a revocation."""


class RefreshTokenReused(DomainError):
    """A retired refresh token was presented outside the grace, or two or more generations old
    (I-24). The login has been deleted — the thief's current token dies with the victim's old one.
    The router must answer this **inside** the request so the deletion commits (technical plan §2)."""


class LoginConcurrentlyRotated(DomainError):
    """`LoginRepository.save_rotation` found the login's `version` moved since it was loaded, or the
    retired row already present (I-25). The repository's word for a lost optimistic-concurrency race;
    `RefreshLogin` translates it to `RefreshInProgress`, never to a revocation or a 500."""


class AccessTokenInvalid(DomainError):
    """`AccessTokenPort.verify` refused a bearer token (I-33 … I-38). Carries the reason for the log
    line; every reason is the same 401 to the client. Never the token."""

    def __init__(self, reason: AccessTokenRefusal) -> None:
        super().__init__(f"access token refused: {reason.value}")
        self.reason = reason


class PasswordHashingFailed(DomainError):
    """`PasswordHasherPort` could not hash or verify — the adapter's `except Exception` floor, so the
    port's promise holds by construction (CLAUDE.md: a port that translates every failure needs a
    catch-all). Carries nothing: a hashing library's message can quote its input."""
