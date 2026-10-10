"""The `User` aggregate: a registered person — one normalized email, one password credential.

**Invariant:** a user has exactly one `EmailAddress` and one `PasswordHash`, and the hash changes only
through `replace_password_hash` (a rehash) or `reset_password` (slice 2.5), never by assignment; and
exactly one `Role`, changed only by `change_role` (slice 4.1, ADR-0032). That is the whole of it, and it is small on
purpose: a `Login` rotates every 15 minutes per tab while a user row is written at registration and on
a rare rehash, so logins are a separate aggregate rather than a collection in here (technical plan
§0.1 — the consistency boundary is the smallest set of things that must change together, and a
rotation never changes the user).

**Not an invariant here: "no two users share an email".** That is the question a reader should ask,
and the answer is the lesson. Uniqueness is a property of the *set* of users, and an aggregate can
only see itself — a `User` checking it would have to load every other user, or call a repository from
inside a constructor, and even then two concurrent registrations would both pass the check before
either inserted. The rule belongs to the one component that sees the whole set atomically: the unique
index `uq_identity_user_email`. `UserRepository.add` translates its violation into
`EmailAlreadyRegistered`, and `ConfirmRegistration` never looks the email up first — the insert *is* the
check (technical plan §0.4; I-5, I-6).

**Not a subtype of anything `GuestSession` is.** Both answer "who is asking", and they share no base
class, `Protocol`, union alias or mixin beyond `RecordsEvents` — which every aggregate composes and
which says nothing about identity (AC-1; CLAUDE.md: shared shape is not shared behaviour; ADR-0010: a
guest session is not a weak login).

**A password is not assumed to be the only credential forever** (ADR-0008's alternatives). The hash
is held under a name that says *this is the password credential*, and the only constructor is
`register_with_password`; an OAuth credential would be additive — a new constructor and a new table —
with nothing here to unpick.
"""

from __future__ import annotations

from datetime import datetime

from tailorcraft.domain.identity.events import (
    PasswordChangedByReset,
    UserPasswordRehashed,
    UserRegistered,
    UserRoleChanged,
)
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, Role, UserId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.events import RecordsEvents


# NOT `slots=True`: this aggregate is mapped by SQLAlchemy's *imperative* mapping
# (`registry.map_imperatively`), which instruments attributes on the instance `__dict__` — a
# `__slots__` class has none, so mapping would fail at wiring time rather than at import time. This
# contradicts the value objects one module over on purpose, as `GuestSession` and `TailoringRun` do.
# The private-attribute names below are exactly what
# `infrastructure/persistence/mapping/identity/user.py` will target, so renaming one here is a
# breaking change to that module too (ADR-0007).
class User(RecordsEvents):
    """A registered person.

    State: `_id`, `_email`, `_password_hash`, `_created_at`, `_password_updated_at`, `_role` —
    private, exposed only through read-only properties. `_password_updated_at` equals `_created_at`
    at registration (the hash was set then) and moves only with `replace_password_hash`. `_role` is
    `Role.USER` at registration and moves only with `change_role`.
    """

    # Class-level annotations only (no assignment): `__init__` sets nothing, so this is how
    # `mypy --strict` learns the types `register_with_password` sets and the properties read back.
    # `_recorded_events` is deliberately not mapped — an in-memory outbox, not a persisted fact.
    _id: UserId
    _email: EmailAddress
    _password_hash: PasswordHash
    _created_at: datetime
    _password_updated_at: datetime
    _role: Role

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build a `User` with `register_with_password`.

        **An empty constructor looks like something to delete; it must stay.** A mapped class that
        defines no `__init__` is given one by `registry.map_imperatively` that accepts the **mapped
        attribute names**, so `User(_email=...)` would be a second constructor that skips
        `register_with_password`, its event and every rule it enforces. A no-argument `__init__`
        makes any argument a `TypeError` again. It must not *raise* either: a mapped class has to be
        built through `cls()`, because SQLAlchemy's instrumentation wrapper is what attaches
        `_sa_instance_state`. `domain/posting/job_posting.py::JobPosting.__init__` carries the full
        account of how the hole was found; `TailoringRun.__init__` carries the rejected alternatives.
        """

    @classmethod
    def register_with_password(
        cls,
        id: UserId,
        email: EmailAddress,
        password_hash: PasswordHash,
        at: datetime,
    ) -> User:
        """The only way to create a `User`: `created_at = password_updated_at = at`.

        The role is always `Role.USER`, and there is **no parameter** for it: no code path registers
        an administrator (AC-2). Promotion is `change_role`, reached only from the operator's CLI.

        Records `UserRegistered(user_id=id, occurred_at=at)`. Takes a `PasswordHash`, never a
        `Password` — hashing is the port's job, done by the use case before this is called, so the
        aggregate never holds a plaintext even for the length of a constructor.
        """
        user = cls()
        user._id = id
        user._email = email
        user._password_hash = password_hash
        user._created_at = at
        user._password_updated_at = at
        user._role = Role.USER
        user.record(UserRegistered(user_id=id, occurred_at=at))
        return user

    def replace_password_hash(self, new: PasswordHash, at: datetime) -> None:
        """Install `new` as the password credential and set `password_updated_at = at`.

        The rehash-on-login path (I-11, ADR-0021): a correct password whose stored hash was made
        under older parameters. Records `UserPasswordRehashed(user_id, occurred_at=at)`. Raises
        `InvariantViolated` if `at` is before `created_at` — a credential cannot be replaced before
        the account existed, and a clock that says otherwise is a bug worth hearing about.
        """
        if at < self._created_at:
            raise InvariantViolated("a password hash cannot be replaced before the user existed")
        self._password_hash = new
        self._password_updated_at = at
        self.record(UserPasswordRehashed(user_id=self._id, occurred_at=at))

    def reset_password(self, new: PasswordHash, at: datetime, logins_revoked: int) -> None:
        """Install `new` — a **different password**, proven by a reset link — as the credential and
        set `password_updated_at = at` (AC-4, ADR-0028).

        Records `PasswordChangedByReset(user_id, logins_revoked, occurred_at=at)`. Raises
        `InvariantViolated` if `at` is before `created_at` (`replace_password_hash`'s guard) or if
        `logins_revoked < 0`.

        **Why the count comes in as an argument.** `ResetPassword` deletes every `Login` of the
        account first — same transaction, so the order is invisible outside it — and hands the
        number here, so the event is recorded by the aggregate like every other event rather than
        built by a use case. The deleting stays the repository's (ADR-0020: revocation is deletion);
        the aggregate only records how many went.

        Not `replace_password_hash` with a flag: that method keeps its rehash meaning (same password,
        today's parameters) and its own event, and a boolean choosing between two events is two
        methods wearing one name.
        """
        # Both refusals before any assignment, so a refused reset changes nothing and records nothing.
        if at < self._created_at:
            raise InvariantViolated("a password cannot be reset before the user existed")
        if logins_revoked < 0:
            raise InvariantViolated("a reset cannot have revoked a negative number of logins")
        self._password_hash = new
        self._password_updated_at = at
        self.record(
            PasswordChangedByReset(user_id=self._id, logins_revoked=logins_revoked, occurred_at=at)
        )

    @property
    def id(self) -> UserId:
        return self._id

    @property
    def email(self) -> EmailAddress:
        return self._email

    @property
    def password_hash(self) -> PasswordHash:
        return self._password_hash

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def password_updated_at(self) -> datetime:
        return self._password_updated_at

    @property
    def role(self) -> Role:
        """This user's one `Role` (slice 4.1)."""
        return self._role

    @property
    def is_admin(self) -> bool:
        """Whether this user is an administrator — **the only role check in the codebase**.

        Asked by `AuthorizeAdministrator` and nowhere else. There is no `is_user` and no check for
        `Role.USER` anywhere, on purpose: "an admin can do everything a user can" is not a rule that
        needs checking, because no user route asks about the role at all — every `/api/me/*` route
        answers to the bearer alone, so an admin passes them by construction (spec, contrast 3). A
        hierarchy (`role >= USER`) would be code guarding a fact that is already true.

        `is`, not `==`: enum members are singletons, and identity says so.
        """
        return self._role is Role.ADMIN

    def change_role(self, to: Role, at: datetime) -> None:
        """Make `to` this user's role, recording `UserRoleChanged`; a no-op when it already is.

        Raises `InvariantViolated` if `at` is before `created_at` — checked before anything else, so a
        refused change neither assigns nor records, even when `to` is already the role. A no-op is
        silent because `UserRoleChanged` is the audit record of who became (or stopped being) an
        administrator; an event for a change that did not happen would make that record lie.
        """
        if at < self._created_at:
            raise InvariantViolated("a role cannot change before the user existed")
        if to is self._role:
            return
        from_role = self._role
        self._role = to
        self.record(
            UserRoleChanged(user_id=self._id, from_role=from_role, to_role=to, occurred_at=at)
        )
