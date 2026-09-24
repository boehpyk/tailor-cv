"""Value objects for the `identity` bounded context.

Two things live here that share nothing but a context. `GuestSessionId` is slice 1.1's: a guest
session is not a login, and shares nothing with `User` (ADR-0008, ADR-0010) — see `docs/adr/0008-*`
for why the two are not unified under a base class even though they sit side by side on the same
tables. Everything below it arrives with slice 2.1 (registration and login, ADR-0020, ADR-0021).

The rule is the one every other context follows: a frozen `@dataclass(frozen=True, slots=True)`
where there is something to validate, a `StrEnum` where the set is closed. Never Pydantic
(ADR-0002).

**Four of these hold a secret or something derived from one** — `Password`, `PasswordHash`,
`TokenHash`, `IssuedAccessToken` — and each redacts its own `repr`. That is not tidiness. A value
object's `repr` is what an f-string, a `logging` call with `%r`, a failed `assert` in pytest and a
Sentry frame all reach for, and "nobody would log that" is a belief; a `repr` that cannot contain the
value is a control. Each also declares the secret field `field(repr=False)`, so that deleting the
hand-written `__repr__` falls back to a generated one that *still* omits it — two locks, because the
hand-written one is the kind of method a reader deletes as "boilerplate".

**Nothing here names argon2, SHA-256 or JWT** (AC-4, AC-7). The domain knows *that* a value is a
hash and what shape a hash has; which algorithm made it is `infrastructure/identity/`'s business.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from tailorcraft.domain.shared.errors import InvariantViolated

# Every function-local `from tailorcraft.domain.identity.errors import ...` below breaks a module
# cycle, exactly as `domain/posting/value_objects.py` does: `errors.py` imports the reason enums from
# this module, so a module-level import in the other direction would make the two import each other
# during collection. `InvariantViolated` lives in `domain.shared` and has no such cycle.

# AC-2's numbers. Module constants, not settings: they are the shape of a mailbox (RFC 5321's
# path and local-part limits), not a knob anybody tunes.
_EMAIL_MAX_LENGTH = 254
_LOCAL_PART_MAX_LENGTH = 64
_DOMAIN_MAX_LENGTH = 253

# PHC strings in practice run ~100 characters; 512 is room for any algorithm's parameters and a
# ceiling on what a corrupt or hostile row can make us carry.
_PASSWORD_HASH_MAX_LENGTH = 512

_TOKEN_HASH_LENGTH = 64
_LOWERCASE_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class GuestSessionId:
    """A `GuestSession`'s identity, typed for the same reason `BaseCvId` is: so a repository method
    or a use case argument cannot silently accept a `BaseCvId` where a session id was meant. The
    authorization rule this slice depends on — `intake_base_cv.guest_session_id == the resolved
    session id` — is exactly the kind of check that a bare `UUID` would let slip past `mypy`."""

    value: UUID

    # No `__post_init__` here on purpose: every `UUID` is already a valid `GuestSessionId`, so a
    # validation method that does nothing would just be a place a future reader adds a rule that
    # does not belong (see the domain-modeler brief). `BaseCvId` is the same shape for the same
    # reason.


# --------------------------------------------------------------------------------------------------
# Slice 2.1 — registered users and their logins.
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserId:
    """A `User`'s identity. Typed so a `LoginId` — or a `GuestSessionId`, which is also "who is
    asking" — cannot be handed where a user id was meant. `UserRepository.get(login.id)` is a lookup
    that quietly finds nothing with bare `UUID`s and a `mypy --strict` error with these."""

    value: UUID

    # No `__post_init__`: every `UUID` is a valid `UserId` (the `GuestSessionId` reasoning above).


@dataclass(frozen=True, slots=True)
class LoginId:
    """A `Login`'s identity — one refresh-token family, one device (ADR-0020)."""

    value: UUID

    # No `__post_init__`: every `UUID` is a valid `LoginId` (the `GuestSessionId` reasoning above).


class InvalidEmailReason(StrEnum):
    """Which rule of AC-2 an email address broke. Closed; carried by `InvalidEmailAddress`.

    **Never reaches the wire.** The API answers every one of these with the single code
    `invalid_email` (I-1, I-12): telling a stranger *which* rule failed helps nobody type an address,
    and the distinction is for the test table and for a developer reading a refusal, not for the
    response body. Carrying a reason rather than a bare error is what lets AC-2's ≥ 20-case table
    assert that `a@b` was refused *for having no dot*, not merely refused — a table that only checks
    "raised" passes against a parser that refuses everything.
    """

    NOT_ASCII = "not_ascii"
    """Any code point above U+007F, anywhere. Internationalized addresses are out of scope in 2.1
    (R-12): refusing them is honest, accepting them un-normalized would make two spellings of one
    mailbox two accounts."""

    WHITESPACE_OR_CONTROL = "whitespace_or_control"
    """Whitespace *inside* the address or any control character. Surrounding whitespace is not a
    refusal — `parse` strips it first."""

    AT_SIGN_COUNT = "at_sign_count"
    """Not exactly one `@` — covers the empty string, `alex.example.com` and `a@@b.com`."""

    LOCAL_PART_LENGTH = "local_part_length"
    """The part before `@` is empty or longer than 64 characters."""

    DOMAIN_LENGTH = "domain_length"
    """The part after `@` is empty or longer than 253 characters."""

    DOMAIN_WITHOUT_DOT = "domain_without_dot"
    """`a@b` — a single-label domain. Legal on an intranet, never a mailbox a stranger can own."""

    EMPTY_DOMAIN_LABEL = "empty_domain_label"
    """`a@.b.com`, `a@b..com`, `a@b.com.` — a dot with nothing on one side of it."""

    TOO_LONG = "too_long"
    """The whole address is longer than 254 characters."""

    NOT_NORMALIZED = "not_normalized"
    """`EmailAddress(...)` was constructed directly with a value `parse` would have changed — upper
    case or surrounding whitespace. Only `__post_init__` raises this; `parse` normalizes first and so
    can never produce it."""


@dataclass(frozen=True, slots=True)
class EmailAddress:
    """A normalized email address: stripped, lower-cased, ASCII, shaped like a mailbox (AC-2).

    **Equality is the uniqueness rule.** `EmailAddress.parse("A@x.io") == EmailAddress.parse(" a@X.io
    ")`, so "one account per address" and "these two are the same address" are the same `==`, and
    the unique index on `identity_user.email` compares exactly the string this type holds.

    `__post_init__` re-validates **and refuses a value that is not already normalized**, so there is
    no second, un-normalized way to hold one: `EmailAddress("Alex@Example.com")` raises
    `InvalidEmailAddress(NOT_NORMALIZED)`. Build one with `parse`. (A repository rehydrating a row
    constructs directly — which is exactly why the direct path must refuse anything `parse` would not
    have produced: the database is also a stranger, one migration later.)

    **Dots and `+tags` are preserved.** `alex.smith+cv@example.com` and `alexsmith@example.com` are
    distinct mailboxes on most providers, and folding them is a provider-specific guess that would
    merge two real people's accounts on the providers where it is wrong.
    """

    value: str

    def __post_init__(self) -> None:
        from tailorcraft.domain.identity.errors import InvalidEmailAddress

        # Normalization first: a value `parse` would have changed is refused as *that*, before any
        # shape rule gets to name a different reason for the same mistake.
        if _normalize_email(self.value) != self.value:
            raise InvalidEmailAddress(InvalidEmailReason.NOT_NORMALIZED)
        reason = _email_refusal(self.value)
        if reason is not None:
            raise InvalidEmailAddress(reason)

    @classmethod
    def parse(cls, raw: str) -> EmailAddress:
        """Strip surrounding whitespace, lower-case, then validate every rule of AC-2.

        Raises `InvalidEmailAddress(reason)` naming the first rule broken. Never carries `raw` in the
        error, not even in its message: a refused address is still somebody's address.
        """
        # One set of rules, applied in one place: `parse` only normalizes, and `__post_init__`
        # validates — so the direct path and this one can never drift apart.
        return cls(_normalize_email(raw))

    @property
    def local_part(self) -> str:
        """Everything before the `@` — what `PasswordPolicy.check` compares a password against,
        beside the whole address (AC-3's "equal to the email or its local part")."""
        # Exactly one `@` is an invariant of this type, so the partition cannot miss.
        local, _, _ = self.value.partition("@")
        return local


def _normalize_email(raw: str) -> str:
    """The whole of `parse`'s normalization: strip, then lower-case. Nothing provider-specific —
    dots and `+tags` survive (the class docstring says why)."""
    return raw.strip().lower()


def _email_refusal(value: str) -> InvalidEmailReason | None:
    """The first AC-2 rule `value` breaks, or `None`. Assumes `value` is already normalized.

    The order is deliberate: character-level rules first (they make every structural rule below
    meaningless), then the `@` that the structure hangs on, then lengths, then the domain's labels.
    """
    if not value.isascii():
        return InvalidEmailReason.NOT_ASCII
    # `isprintable()` is False for every ASCII control character; `" "` is the only printable
    # whitespace in ASCII. Surrounding whitespace never reaches here — normalization stripped it.
    if " " in value or not value.isprintable():
        return InvalidEmailReason.WHITESPACE_OR_CONTROL
    if value.count("@") != 1:
        return InvalidEmailReason.AT_SIGN_COUNT
    if len(value) > _EMAIL_MAX_LENGTH:
        return InvalidEmailReason.TOO_LONG
    local, _, domain = value.partition("@")
    if not 1 <= len(local) <= _LOCAL_PART_MAX_LENGTH:
        return InvalidEmailReason.LOCAL_PART_LENGTH
    if not 1 <= len(domain) <= _DOMAIN_MAX_LENGTH:
        return InvalidEmailReason.DOMAIN_LENGTH
    if "." not in domain:
        return InvalidEmailReason.DOMAIN_WITHOUT_DOT
    if "" in domain.split("."):
        return InvalidEmailReason.EMPTY_DOMAIN_LABEL
    return None


class WeakPasswordReason(StrEnum):
    """Why a password was refused. Closed; carried by `WeakPassword`, and — unlike
    `InvalidEmailReason` — **each member is its own wire code** (I-2, I-3, I-4), because the user
    can act on the difference: "too short" and "same as your email" ask for different fixes."""

    TOO_SHORT = "password_too_short"
    TOO_LONG = "password_too_long"
    MATCHES_EMAIL = "password_matches_email"


# The hashing-DoS bound (AC-3, I-3). Applies to *every* password that enters the system — login as
# well as registration — because it is what stops a megabyte "password" reaching the hasher, and a
# login is the endpoint a stranger can call without an account. Not the policy's `max_length`: that
# one is a registration rule about what a *good* password is; this one is about what the process can
# afford to hash. A module constant rather than a setting for the codebase's usual reason — a control
# does not get a knob (ADR-0012).
PASSWORD_INPUT_MAX_LENGTH = 1024


@dataclass(frozen=True, slots=True)
class Password:
    """A plaintext password, NFKC-normalized, that cannot be printed (AC-3).

    `from_input` is the way in: NFKC (NIST SP 800-63B §5.1.1.2, so a password typed on two keyboards
    that produce two encodings of one character is one password), then refuse an empty value and
    anything over `PASSWORD_INPUT_MAX_LENGTH` code points. **No whitespace is stripped** — a trailing
    space is a character the user typed, and silently removing it would make "correct password,
    refused" a support ticket nobody can reproduce.

    `__post_init__` re-checks the same bounds and refuses a value that is not already NFKC, for
    `EmailAddress`'s reason: one form, no side door. That refusal is `InvariantViolated`, not
    `WeakPassword` — no user input can produce it (see the comment at the check).

    **Why a type at all**, when every use case could hold a `str`: `repr()`, `str()` and `format()`
    all return `Password(<redacted>)`, so `f"{password}"`, `log.info("%s", password)` and a pytest
    assertion diff cannot put a plaintext into a log line; and `mypy` refuses a `Password` where any
    other `str` is expected, so it cannot be confused with the email beside it in the same request.

    `from_input` raises `WeakPassword` for its two refusals — `TOO_SHORT` for empty, `TOO_LONG` over
    the bound — carrying `min_length=1` and `max_length=PASSWORD_INPUT_MAX_LENGTH`, i.e. the bounds it
    actually applied, never the policy's. A registration password of 129…1024 code points passes
    here and is refused by `PasswordPolicy` naming 128; on login these are the only length rules
    there are (and the HTTP body's own 1024 cap normally refuses first — this is the second lock).
    """

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        from tailorcraft.domain.identity.errors import WeakPassword

        def refuse(reason: WeakPasswordReason) -> WeakPassword:
            return WeakPassword(reason, min_length=1, max_length=PASSWORD_INPUT_MAX_LENGTH)

        # Length before normalization: an over-long value is refused before it is normalized, so a
        # megabyte cannot be made to cost an NFKC pass on the direct path either. (`from_input`
        # normalizes first; NFKC can lengthen a string, so the bound it applies is to the result.)
        if not self.value:
            raise refuse(WeakPasswordReason.TOO_SHORT)
        if len(self.value) > PASSWORD_INPUT_MAX_LENGTH:
            raise refuse(WeakPasswordReason.TOO_LONG)
        # Not NFKC is a broken invariant, not a weak password: `from_input` normalizes first and
        # NFKC is idempotent, so no user input can reach this line — only a caller that bypassed
        # `from_input` (an adapter, a test, a corrupted row). That is `EmailAddress`'s, `PasswordHash`'s
        # and `TokenHash`'s answer to a directly-constructed value in the wrong form, and it keeps
        # `WeakPasswordReason` to the three codes a person can act on — each of them a wire code.
        # The message names the rule, never the value.
        if not unicodedata.is_normalized("NFKC", self.value):
            raise InvariantViolated("a password must already be NFKC-normalized")

    @classmethod
    def from_input(cls, raw: str) -> Password:
        """NFKC-normalize `raw` without stripping it, and refuse empty or over-long input.

        Raises `WeakPassword(TOO_SHORT | TOO_LONG, min_length=1,
        max_length=PASSWORD_INPUT_MAX_LENGTH)`. Never carries the value or its length.
        """
        # Deliberately no `.strip()` — see the class docstring. `__post_init__` applies the bounds
        # to the normalized value, which is what every later length rule counts.
        return cls(unicodedata.normalize("NFKC", raw))

    def __repr__(self) -> str:
        return "Password(<redacted>)"

    def __str__(self) -> str:
        return "Password(<redacted>)"

    def __format__(self, format_spec: str) -> str:
        # `object.__format__` would delegate to `__str__` for an empty spec but raise `TypeError`
        # for any other — so `f"{password:>20}"` would crash rather than redact. Defined explicitly
        # so every spec redacts: the spec is applied to the redacted form, never to the value.
        return format(str(self), format_spec)


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """What registration accepts as a password (AC-3, OQ-3): 12 to 128 code points after NFKC, and
    not the email or its local part. **No composition rules** — NIST SP 800-63B §5.1.1.2 advises
    against them, and `aaaaaaaaaaaa` passing is a test, not an oversight.

    **The policy is data** so the API can put `min_length` / `max_length` in the 422 envelope from the
    same object that refused the password; the client never hard-codes 12 as a rule, only as copy.

    Used **only at registration**. A login checks the password it is given against the stored hash
    and nothing else: a policy tightened next year must not lock out the people who registered under
    this one.
    """

    min_length: int = 12
    max_length: int = 128

    def __post_init__(self) -> None:
        """Refuse a policy that could never be met or that exceeds the input bound:
        `1 <= min_length <= max_length <= PASSWORD_INPUT_MAX_LENGTH`."""
        if not 1 <= self.min_length <= self.max_length <= PASSWORD_INPUT_MAX_LENGTH:
            raise InvariantViolated(
                "password policy bounds must satisfy "
                f"1 <= min_length <= max_length <= {PASSWORD_INPUT_MAX_LENGTH}"
            )

    def check(self, password: Password, email: EmailAddress) -> None:
        """Return nothing if `password` is acceptable for `email`; otherwise raise
        `WeakPassword(reason, self.min_length, self.max_length)`.

        Lengths are in code points of the (already NFKC) value. "Matches the email" is
        case-insensitive and compares against both the whole address and its local part.
        """
        from tailorcraft.domain.identity.errors import WeakPassword

        # `len` of a `str` is code points, and `Password` guarantees the value is already NFKC —
        # so a ligature that expands to two letters counts as two, never one.
        length = len(password.value)
        if length < self.min_length:
            raise WeakPassword(WeakPasswordReason.TOO_SHORT, self.min_length, self.max_length)
        if length > self.max_length:
            raise WeakPassword(WeakPasswordReason.TOO_LONG, self.min_length, self.max_length)
        # `casefold`, not `lower`: it is the comparison Unicode defines for "ignoring case". The
        # email is ASCII and already lower-case, so only the password side needs folding.
        if password.value.casefold() in (email.value, email.local_part):
            raise WeakPassword(WeakPasswordReason.MATCHES_EMAIL, self.min_length, self.max_length)


@dataclass(frozen=True, slots=True)
class PasswordHash:
    """A stored password hash in PHC string format (AC-4): non-empty, `$`-prefixed, ≤ 512.

    PHC (`$<id>$<params>$<salt>$<hash>`) is a vendor-neutral standard, not an argon2 fact, which is
    why the domain may check for the `$` without knowing the algorithm: the id and the parameters
    live *inside* the string, so a parameter change (ADR-0021's rehash-on-login) needs no column and
    no migration. The `repr` is redacted — a hash is not a password, but an offline guessing attack
    needs nothing else.
    """

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        # Messages name the rule, never the value: a malformed hash is still most of a hash.
        if not self.value.startswith("$"):
            raise InvariantViolated("a password hash must be a non-empty PHC string")
        if len(self.value) > _PASSWORD_HASH_MAX_LENGTH:
            raise InvariantViolated(
                f"a password hash must be at most {_PASSWORD_HASH_MAX_LENGTH} characters"
            )

    def __repr__(self) -> str:
        return "PasswordHash(<redacted>)"


@dataclass(frozen=True, slots=True)
class TokenHash:
    """The hash of a refresh token: exactly 64 lowercase hex characters (AC-4).

    **The type that makes "application code never sees a plaintext refresh token" checkable by
    `mypy`.** The route mints `(token, hash)` and hands the application only the hash (ADR-0010's
    pattern for the guest cookie, ADR-0020); every application signature that touches a refresh
    token takes a `TokenHash`, so passing the plaintext is a type error rather than a review comment.
    The `repr` is redacted: it is a lookup key into `identity_login`, and a log line holding one is
    one half of a session.
    """

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        # A set test, not `int(value, 16)`: that would accept upper case, a `0x` prefix, `_`
        # separators and surrounding whitespace, none of which a stored hash may have.
        if len(self.value) != _TOKEN_HASH_LENGTH or not set(self.value) <= _LOWERCASE_HEX:
            raise InvariantViolated(
                f"a token hash must be exactly {_TOKEN_HASH_LENGTH} lowercase hex characters"
            )

    def __repr__(self) -> str:
        return "TokenHash(<redacted>)"


@dataclass(frozen=True, slots=True)
class RetiredRefreshToken:
    """A refresh token that was current until a rotation replaced it (ADR-0020's option (c)).

    Returned by `Login.rotate` and persisted by `LoginRepository.save_rotation` into the append-only
    retired table — a lookup index answering "whose token was this, and which generation?", which is
    what lets reuse be detected *at any generation* rather than only the immediate predecessor.
    `generation >= 1`, because generation 1 is the first one `Login.start` issues.
    """

    token_hash: TokenHash
    generation: int
    retired_at: datetime

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise InvariantViolated("a retired refresh token's generation must be at least 1")


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    """A freshly minted access token and how long it lives, as the client will receive it.

    **`expires_in` is relative** (a `timedelta`, positive), never an absolute instant: the client is
    told "this lives 900 s", so its own clock — which may be anywhere — never enters the arithmetic
    (I-38, AC-38). The token is opaque to the domain (`AccessTokenPort` knows its format; this type
    does not), and its `repr` is redacted because a bearer token in a log line is a login.
    """

    token: str = field(repr=False)
    expires_in: timedelta

    def __post_init__(self) -> None:
        if self.expires_in <= timedelta(0):
            raise InvariantViolated("an access token's lifetime must be positive")

    def __repr__(self) -> str:
        # The lifetime is not a secret and is what a reader debugging a 401 wants to see.
        return f"IssuedAccessToken(token=<redacted>, expires_in={self.expires_in!r})"


class PasswordVerdict(StrEnum):
    """What `PasswordHasherPort.verify` found. **Three outcomes, not a `bool` plus a side channel.**

    `MATCH_NEEDS_REHASH` is a match whose stored hash was made with parameters older than today's;
    `LogIn` replaces the hash in the same unit of work (I-11). A `bool` return with a separate
    `needs_rehash()` call would be two questions about one comparison, and a caller that asked only
    the first would silently never upgrade anybody.
    """

    MATCH = "match"
    MATCH_NEEDS_REHASH = "match_needs_rehash"
    MISMATCH = "mismatch"


class RetiredTokenVerdict(StrEnum):
    """What `Login.judge_retired` decided about a retired token presented again (AC-6).

    `RACED`: the immediate predecessor, within `REFRESH_RACE_GRACE` of the rotation — a second tab
    that lost the race, answered 409 with nothing changed (I-23). `REUSED`: anything else — the
    login is revoked (I-24).
    """

    RACED = "raced"
    REUSED = "reused"


class AccessTokenRefusal(StrEnum):
    """Why `AccessTokenPort.verify` refused a bearer token (I-33 … I-38). Closed; carried by
    `AccessTokenInvalid`, logged as `reason=`, and **never** sent to the client — every one of these
    is the same 401 `invalid_access_token`, so a forger learns nothing about which check caught them.
    """

    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    BAD_ALGORITHM = "bad_algorithm"
    BAD_CLAIMS = "bad_claims"
    EXPIRED = "expired"
    ISSUED_IN_FUTURE = "issued_in_future"


class LoginNotFoundReason(StrEnum):
    """Why a refresh answered `LoginNotFound` (I-20, I-21). Closed; carried by `LoginNotFound`, logged
    as `identity.refresh_refused reason=`, and **never** sent to the client — both are the same 401
    `not_signed_in`, because after a revocation "gone" and "never existed" are indistinguishable by
    design (ADR-0020).

    `UNKNOWN`: the token matched no live login — forged, revoked, or never issued (I-20); there is no
    login to name. `EXPIRED`: it matched a login at or past its absolute `expires_at`, which was deleted
    on sight (I-21); the login id is known and logged.
    """

    UNKNOWN = "unknown"
    EXPIRED = "expired"
