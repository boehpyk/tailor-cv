"""The `User` aggregate: AC-5 (what `register_with_password` and `replace_password_hash` record)
and AC-1's mapped-class-constructor hole, restated for `User` (task-list.md T8: `User(_email=…)`
must raise `TypeError`).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Every assertion here comes from
`domain/identity/user.py`'s own docstrings and AC-5/AC-6's text, not from running the (currently
unimplemented) methods and recording what they did.

There is no other way into a real `User` than `register_with_password` — that absence is this
aggregate's whole invariant (technical plan §1: "the only constructor") — so every fixture below is
built through it rather than by touching a private attribute.

**RED-first trap** (task-list.md, top of file): `NotImplementedError` subclasses `RuntimeError`, so
every `pytest.raises(...)` below names the exact domain exception, never `RuntimeError` or
`Exception`. Value objects (`EmailAddress`, `PasswordHash`) are themselves still skeletons at this
point in the build order (T6 GREEN has not landed), so most tests below actually fail one layer
down, inside `EmailAddress.__post_init__` or `PasswordHash.__post_init__` — still `NotImplementedError`,
still not an `ImportError`, still the right kind of red for a slice with more than one unimplemented
layer at once. The two constructor-hole tests at the bottom use placeholder strings instead of real
value objects for exactly this reason: they are testing `__init__`'s own signature, not `User`'s
business rules, and must stay green on arrival regardless of what else is still unimplemented.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.events import UserPasswordRehashed, UserRegistered
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tailorcraft.domain.shared.errors import InvariantViolated

_USER_ID = UserId(value=UUID("44444444-4444-7444-8444-444444444444"))
_CREATED_AT = datetime(2026, 9, 23, 10, 0, 0, tzinfo=UTC)  # whole-second, ADR-0007

_PHC_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"
_OTHER_PHC_HASH = "$argon2id$v=19$m=65536,t=3,p=4$bmV3c2FsdA$bmV3aGFzaGJ5dGVz"


def _email(raw: str = "alex@example.com") -> EmailAddress:
    return EmailAddress.parse(raw)


def _hash(value: str = _PHC_HASH) -> PasswordHash:
    return PasswordHash(value)


def _registered(*, at: datetime = _CREATED_AT) -> User:
    """The only constructor, so every other builder below starts here (AC-5, technical plan §1)."""
    return User.register_with_password(_USER_ID, _email(), _hash(), at)


# --- AC-5: register_with_password records exactly UserRegistered, and sets both instants equal ----


def test_register_with_password_records_exactly_one_user_registered() -> None:
    user = _registered()

    events = user.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, UserRegistered)
    assert event.user_id == _USER_ID
    assert event.occurred_at == _CREATED_AT


def test_register_with_password_sets_created_at_and_password_updated_at_equal() -> None:
    """`_password_updated_at` equals `_created_at` at registration — the hash was set then
    (`user.py`'s own docstring)."""
    user = _registered()

    assert user.created_at == _CREATED_AT
    assert user.password_updated_at == _CREATED_AT


def test_register_with_password_sets_the_id_email_and_hash() -> None:
    email = _email("someone@example.com")
    hashed = _hash()

    user = User.register_with_password(_USER_ID, email, hashed, _CREATED_AT)

    assert user.id == _USER_ID
    assert user.email == email
    assert user.password_hash == hashed


# --- AC-5: replace_password_hash records exactly UserPasswordRehashed -----------------------------


def test_replace_password_hash_records_exactly_one_user_password_rehashed() -> None:
    user = _registered()
    user.release_events()  # discard UserRegistered so this test sees only what the rehash adds
    rehashed_at = _CREATED_AT + timedelta(days=30)

    user.replace_password_hash(_hash(_OTHER_PHC_HASH), rehashed_at)

    events = user.release_events()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, UserPasswordRehashed)
    assert event.user_id == _USER_ID
    assert event.occurred_at == rehashed_at


def test_replace_password_hash_replaces_the_hash_and_moves_password_updated_at() -> None:
    user = _registered()
    new_hash = _hash(_OTHER_PHC_HASH)
    rehashed_at = _CREATED_AT + timedelta(days=30)

    user.replace_password_hash(new_hash, rehashed_at)

    assert user.password_hash == new_hash
    assert user.password_updated_at == rehashed_at
    assert user.created_at == _CREATED_AT  # created_at never moves (I-11, ADR-0021)


def test_replace_password_hash_refuses_an_instant_before_created_at() -> None:
    """A credential cannot be replaced before the account existed, and a clock that says otherwise
    is a bug worth hearing about (`user.py`'s own docstring)."""
    user = _registered()
    before_creation = _CREATED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        user.replace_password_hash(_hash(_OTHER_PHC_HASH), before_creation)


def test_replace_password_hash_at_exactly_created_at_succeeds() -> None:
    """The boundary the refusal above does not cover: `at == created_at` is not "before"."""
    user = _registered()

    user.replace_password_hash(_hash(_OTHER_PHC_HASH), _CREATED_AT)  # must not raise

    assert user.password_updated_at == _CREATED_AT


# --- AC-1 / CLAUDE.md: the mapped-class default-constructor hole, restated for User ----------------
#
# Both green on arrival: `__init__` takes no arguments today, so Python's own signature check
# refuses both calls before any body runs — see `test_tailoring_run.py`'s identical pair, the
# precedent this mirrors. Placeholder strings stand in for real value objects deliberately: these
# tests are about the constructor's signature, not about `User`'s business rules, so they must not
# depend on `EmailAddress` or `PasswordHash` being implemented.


def test_user_cannot_be_constructed_with_the_register_arguments() -> None:
    """`register_with_password` is the only constructor. A direct call — even with every argument
    it itself needs — must be refused, because a second way in is a second place the invariant
    could be bypassed."""
    with pytest.raises(TypeError):
        User(  # type: ignore[call-arg]
            id=_USER_ID,
            email="placeholder@example.com",
            password_hash="placeholder-hash",
            at=_CREATED_AT,
        )


def test_user_cannot_be_constructed_via_the_mapped_attribute_names() -> None:
    """The hole task-list.md T8 names directly: `registry.map_imperatively` installs a default
    constructor accepting the **mapped** attribute names on any mapped class with no `__init__` of
    its own — so without the explicit no-argument `__init__` in the skeleton, `User(_email=...)`
    would be a second, uninvariant-checked way to build one: no id, no password hash, and an email
    that never went through `register_with_password`'s event. This test is what keeps that
    empty-looking `__init__` from being deleted as dead code."""
    with pytest.raises(TypeError):
        User(_email="placeholder")  # type: ignore[call-arg]
