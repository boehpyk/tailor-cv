"""Slice 4.2 (ADR-0033), the disabled account: AC-1, AC-2 (a)(b)(c) and AC-3.

Pure domain tests: no I/O, no fixtures, no mocks. Expected reds against the T2 skeleton are
`NotImplementedError` from `User.is_disabled` / `disable` / `enable`; no test uses
`pytest.raises(RuntimeError)`, which a `NotImplementedError` would satisfy vacuously.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.events import UserDisabled, UserEnabled
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tailorcraft.domain.shared.errors import InvariantViolated

_USER_ID = UserId(value=UUID("55555555-5555-7555-8555-555555555555"))
_CREATED_AT = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)  # whole-second, ADR-0007
_LATER = _CREATED_AT + timedelta(hours=1)
_LATEST = _CREATED_AT + timedelta(hours=2)
_BEFORE = _CREATED_AT - timedelta(seconds=1)
_PHC_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"


def _registered() -> User:
    user = User.register_with_password(
        _USER_ID, EmailAddress.parse("alex@example.com"), PasswordHash(_PHC_HASH), _CREATED_AT
    )
    user.release_events()
    return user


def _disabled() -> User:
    user = _registered()
    user.disable(_LATER, 2)
    user.release_events()
    return user


# --- AC-1 ------------------------------------------------------------------------------------


def test_a_newly_registered_user_is_not_disabled() -> None:
    user = _registered()
    assert user.disabled_at is None
    assert user.is_disabled is False


# --- AC-2 (a) --------------------------------------------------------------------------------


def test_disabling_an_enabled_user_sets_disabled_at() -> None:
    user = _registered()
    user.disable(_LATER, 3)
    assert user.disabled_at == _LATER
    assert user.is_disabled is True


def test_disabling_an_enabled_user_records_exactly_one_event_with_the_payload() -> None:
    user = _registered()
    user.disable(_LATER, 3)
    assert user.release_events() == (
        UserDisabled(user_id=_USER_ID, logins_revoked=3, occurred_at=_LATER),
    )


def test_disabling_with_zero_logins_revoked_is_allowed() -> None:
    user = _registered()
    user.disable(_LATER, 0)
    assert user.is_disabled is True


def test_disabling_at_the_creation_instant_is_allowed() -> None:
    user = _registered()
    user.disable(_CREATED_AT, 0)
    assert user.disabled_at == _CREATED_AT


# --- AC-2 (b) --------------------------------------------------------------------------------


def test_disabling_a_disabled_user_keeps_the_original_disabled_at() -> None:
    user = _disabled()
    user.disable(_LATEST, 5)
    assert user.disabled_at == _LATER


def test_disabling_a_disabled_user_records_nothing() -> None:
    user = _disabled()
    user.disable(_LATEST, 5)
    assert user.release_events() == ()


# --- AC-2 (c) --------------------------------------------------------------------------------


def test_disabling_before_creation_is_refused_and_assigns_nothing() -> None:
    user = _registered()
    with pytest.raises(InvariantViolated):
        user.disable(_BEFORE, 0)
    assert user.disabled_at is None
    assert user.release_events() == ()


def test_disabling_with_a_negative_revoked_count_is_refused_and_assigns_nothing() -> None:
    user = _registered()
    with pytest.raises(InvariantViolated):
        user.disable(_LATER, -1)
    assert user.disabled_at is None
    assert user.release_events() == ()


def test_disabling_a_disabled_user_before_creation_is_refused_not_silently_ignored() -> None:
    user = _disabled()
    with pytest.raises(InvariantViolated):
        user.disable(_BEFORE, 0)


def test_disabling_a_disabled_user_with_a_negative_count_is_refused_not_silently_ignored() -> None:
    user = _disabled()
    with pytest.raises(InvariantViolated):
        user.disable(_LATEST, -1)


# --- AC-3 ------------------------------------------------------------------------------------


def test_enabling_a_disabled_user_clears_disabled_at() -> None:
    user = _disabled()
    user.enable(_LATEST)
    assert user.disabled_at is None
    assert user.is_disabled is False


def test_enabling_a_disabled_user_records_exactly_one_event() -> None:
    user = _disabled()
    user.enable(_LATEST)
    assert user.release_events() == (UserEnabled(user_id=_USER_ID, occurred_at=_LATEST),)


def test_enabling_an_enabled_user_is_a_silent_no_op() -> None:
    user = _registered()
    user.enable(_LATER)
    assert user.is_disabled is False
    assert user.release_events() == ()


def test_enabling_before_creation_is_refused_and_leaves_the_user_disabled() -> None:
    user = _disabled()
    with pytest.raises(InvariantViolated):
        user.enable(_BEFORE)
    assert user.disabled_at == _LATER
    assert user.release_events() == ()


def test_enabling_an_enabled_user_before_creation_is_refused_not_silently_ignored() -> None:
    user = _registered()
    with pytest.raises(InvariantViolated):
        user.enable(_BEFORE)


def test_a_user_can_be_disabled_again_after_being_enabled() -> None:
    user = _disabled()
    user.enable(_LATEST)
    user.release_events()
    later_still = _LATEST + timedelta(hours=1)
    user.disable(later_still, 1)
    assert user.disabled_at == later_still
    assert user.release_events() == (
        UserDisabled(user_id=_USER_ID, logins_revoked=1, occurred_at=later_still),
    )
