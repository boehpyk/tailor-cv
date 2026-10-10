"""Slice 4.1 (ADR-0032), the `User` role: AC-2, AC-3 (a)(b)(c) and AC-4.

Pure domain tests: no I/O, no fixtures, no mocks. Every assertion comes from the spec's acceptance
criteria, not from running the code. Expected reds against the T2 skeleton are `NotImplementedError`
from `User.role` / `User.is_admin` / `User.change_role` (accepted on 2.3/2.4's precedent); no test
uses `pytest.raises(RuntimeError)`, which a `NotImplementedError` would satisfy vacuously.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.events import UserRoleChanged
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, Role, UserId
from tailorcraft.domain.shared.errors import InvariantViolated

_USER_ID = UserId(value=UUID("44444444-4444-7444-8444-444444444444"))
_CREATED_AT = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)  # whole-second, ADR-0007
_LATER = _CREATED_AT + timedelta(hours=1)
_PHC_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"


def _registered() -> User:
    user = User.register_with_password(
        _USER_ID, EmailAddress.parse("alex@example.com"), PasswordHash(_PHC_HASH), _CREATED_AT
    )
    user.release_events()  # discard UserRegistered so tests see only what the role change adds
    return user


# --- AC-2 ------------------------------------------------------------------------------------


def test_a_newly_registered_user_has_the_user_role() -> None:
    assert _registered().role is Role.USER


def test_a_newly_registered_user_is_not_an_admin() -> None:
    assert _registered().is_admin is False


# --- AC-3 (a): a different role --------------------------------------------------------------


def test_change_role_to_a_different_role_sets_it() -> None:
    user = _registered()

    user.change_role(Role.ADMIN, _LATER)

    assert user.role is Role.ADMIN


def test_change_role_to_a_different_role_records_exactly_one_user_role_changed() -> None:
    user = _registered()

    user.change_role(Role.ADMIN, _LATER)

    (event,) = user.release_events()
    assert isinstance(event, UserRoleChanged)
    assert event.user_id == _USER_ID
    assert event.from_role is Role.USER
    assert event.to_role is Role.ADMIN
    assert event.occurred_at == _LATER


def test_change_role_back_records_the_reverse_transition() -> None:
    user = _registered()
    user.change_role(Role.ADMIN, _LATER)
    user.release_events()

    user.change_role(Role.USER, _LATER + timedelta(seconds=1))

    (event,) = user.release_events()
    assert isinstance(event, UserRoleChanged)
    assert (event.from_role, event.to_role) == (Role.ADMIN, Role.USER)
    assert user.role is Role.USER


def test_change_role_at_exactly_created_at_succeeds() -> None:
    user = _registered()

    user.change_role(Role.ADMIN, _CREATED_AT)

    assert user.role is Role.ADMIN


# --- AC-3 (b): the same role is a no-op ------------------------------------------------------


def test_change_role_to_the_same_role_records_nothing() -> None:
    user = _registered()

    user.change_role(Role.USER, _LATER)

    assert user.release_events() == ()
    assert user.role is Role.USER


def test_change_role_to_admin_twice_records_only_the_first_change() -> None:
    user = _registered()
    user.change_role(Role.ADMIN, _LATER)
    user.release_events()

    user.change_role(Role.ADMIN, _LATER + timedelta(seconds=1))

    assert user.release_events() == ()
    assert user.role is Role.ADMIN


# --- AC-3 (c): an instant before created_at --------------------------------------------------


def test_change_role_refuses_an_instant_before_created_at() -> None:
    user = _registered()

    with pytest.raises(InvariantViolated):
        user.change_role(Role.ADMIN, _CREATED_AT - timedelta(seconds=1))


def test_a_refused_change_role_changes_and_records_nothing() -> None:
    """The T3 RED failed on the skeleton's NotImplementedError, so this was proven at /verify:
    moving the guard below the assignment and `record` fails it (1 failed, 27 passed), source
    restored byte-exact."""
    user = _registered()

    with pytest.raises(InvariantViolated):
        user.change_role(Role.ADMIN, _CREATED_AT - timedelta(seconds=1))

    assert user.role is Role.USER
    assert user.release_events() == ()


# --- AC-4: is_admin follows role -------------------------------------------------------------


def test_is_admin_flips_both_ways_through_change_role() -> None:
    user = _registered()
    seen = [user.is_admin]

    user.change_role(Role.ADMIN, _LATER)
    seen.append(user.is_admin)

    user.change_role(Role.USER, _LATER + timedelta(seconds=1))
    seen.append(user.is_admin)

    assert seen == [False, True, False]


# --- AC-1 (test-after) -----------------------------------------------------------------------


def test_role_has_exactly_user_then_admin_with_their_wire_values() -> None:
    assert [(m.name, m.value) for m in Role] == [("USER", "user"), ("ADMIN", "admin")]


@pytest.mark.parametrize("spelling", ["ROLE_ADMIN", "Admin", ""])
def test_role_refuses_a_spelling_that_is_not_a_value(spelling: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the message is stdlib's, not ours
        Role(spelling)


def test_role_round_trips_its_values() -> None:
    assert (Role("user"), Role("admin")) == (Role.USER, Role.ADMIN)
