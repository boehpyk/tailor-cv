"""AC-2: the `PendingRegistration` aggregate — request, issue once, confirm, inclusive expiry.

Pure domain tests. Every assertion comes from AC-2 and the aggregate's docstrings. Against the
skeleton every aggregate method raises `NotImplementedError`, which is the expected red here; each
`pytest.raises` names the exact domain error because `NotImplementedError` is a `RuntimeError`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.errors import (
    PendingRegistrationAlreadyIssued,
    PendingRegistrationExpired,
    PendingRegistrationNotIssued,
)
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    PendingRegistrationId,
    TokenHash,
)
from tailorcraft.domain.shared.errors import InvariantViolated

_ID = PendingRegistrationId(value=UUID("55555555-5555-7555-8555-555555555555"))
_AT = datetime(2026, 10, 2, 10, 0, 0, tzinfo=UTC)  # whole-second, ADR-0007
_TTL = timedelta(hours=24)
_PHC = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"
_TOKEN_HASH = TokenHash("ab" * 32)
_OTHER_TOKEN_HASH = TokenHash("cd" * 32)


def _email() -> EmailAddress:
    return EmailAddress.parse("alex@example.com")


def _requested(ttl: timedelta = _TTL) -> PendingRegistration:
    return PendingRegistration.request(_ID, _email(), PasswordHash(_PHC), _AT, ttl)


def _issued() -> PendingRegistration:
    pending = _requested()
    pending.issue(_TOKEN_HASH, _AT + timedelta(seconds=5))
    return pending


# --- request -----------------------------------------------------------------------------------


def test_request_builds_a_pending_registration_that_is_not_issued() -> None:
    pending = _requested()

    assert pending.id == _ID
    assert pending.email == _email()
    assert pending.password_hash == PasswordHash(_PHC)
    assert pending.requested_at == _AT
    assert pending.token_hash is None
    assert pending.issued_at is None


def test_request_sets_expires_at_to_the_instant_plus_the_ttl() -> None:
    assert _requested(timedelta(hours=3)).expires_at == _AT + timedelta(hours=3)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1), timedelta(days=-1)])
def test_request_refuses_a_ttl_that_is_not_positive(ttl: timedelta) -> None:
    with pytest.raises(InvariantViolated):
        _requested(ttl)


def test_request_records_no_events() -> None:
    assert _requested().release_events() == ()


# --- is_expired: inclusive ---------------------------------------------------------------------


def test_is_not_expired_one_second_before_expires_at() -> None:
    assert _requested().is_expired(_AT + _TTL - timedelta(seconds=1)) is False


def test_is_expired_exactly_at_expires_at() -> None:
    assert _requested().is_expired(_AT + _TTL) is True


def test_is_expired_after_expires_at() -> None:
    assert _requested().is_expired(_AT + _TTL + timedelta(seconds=1)) is True


# --- issue -------------------------------------------------------------------------------------


def test_issue_sets_the_token_hash_and_the_issued_at() -> None:
    pending = _requested()
    issued_at = _AT + timedelta(minutes=1)

    pending.issue(_TOKEN_HASH, issued_at)

    assert pending.token_hash == _TOKEN_HASH
    assert pending.issued_at == issued_at


def test_issue_leaves_requested_at_and_expires_at_alone() -> None:
    pending = _requested()

    pending.issue(_TOKEN_HASH, _AT + timedelta(minutes=1))

    assert pending.requested_at == _AT
    assert pending.expires_at == _AT + _TTL


def test_issue_refuses_an_expired_registration() -> None:
    with pytest.raises(PendingRegistrationExpired):
        _requested().issue(_TOKEN_HASH, _AT + _TTL)  # inclusive: the boundary itself is expired


def test_a_refused_issue_leaves_the_registration_unissued() -> None:
    pending = _requested()

    with pytest.raises(PendingRegistrationExpired):
        pending.issue(_TOKEN_HASH, _AT + _TTL)

    assert pending.token_hash is None
    assert pending.issued_at is None


def test_issue_refuses_a_second_time() -> None:
    pending = _issued()

    with pytest.raises(PendingRegistrationAlreadyIssued):
        pending.issue(_OTHER_TOKEN_HASH, _AT + timedelta(seconds=10))


def test_a_refused_second_issue_keeps_the_first_token_hash_and_instant() -> None:
    pending = _issued()
    first_issued_at = pending.issued_at

    with pytest.raises(PendingRegistrationAlreadyIssued):
        pending.issue(_OTHER_TOKEN_HASH, _AT + timedelta(seconds=10))

    assert pending.token_hash == _TOKEN_HASH
    assert pending.issued_at == first_issued_at


def test_issue_records_no_events() -> None:
    assert _issued().release_events() == ()


# --- confirm -----------------------------------------------------------------------------------


def test_confirm_returns_the_email_and_password_hash_a_user_is_built_from() -> None:
    pending = _issued()

    email, password_hash = pending.confirm(_AT + timedelta(hours=1))

    assert email == _email()
    assert password_hash == PasswordHash(_PHC)


def test_confirm_refuses_a_registration_that_was_never_issued() -> None:
    with pytest.raises(PendingRegistrationNotIssued):
        _requested().confirm(_AT + timedelta(minutes=1))


def test_confirm_refuses_an_expired_registration() -> None:
    pending = _issued()

    with pytest.raises(PendingRegistrationExpired):
        pending.confirm(_AT + _TTL)  # inclusive


def test_confirm_succeeds_one_second_before_expiry() -> None:
    email, _ = _issued().confirm(_AT + _TTL - timedelta(seconds=1))

    assert email == _email()


def test_confirm_records_no_events() -> None:
    pending = _issued()

    pending.confirm(_AT + timedelta(hours=1))

    assert pending.release_events() == ()


# --- the invariant: token_hash is None  <=>  issued_at is None ---------------------------------


def test_token_hash_and_issued_at_are_both_none_before_issue_and_both_set_after() -> None:
    pending = _requested()
    assert (pending.token_hash is None) == (pending.issued_at is None) is True

    pending.issue(_TOKEN_HASH, _AT + timedelta(seconds=1))

    assert pending.token_hash is not None
    assert pending.issued_at is not None
    assert _AT <= pending.issued_at < pending.expires_at


# --- the mapped-class constructor hole (User's AC-1, restated) ---------------------------------


def test_the_mapped_attribute_names_are_not_a_constructor() -> None:
    with pytest.raises(TypeError):
        PendingRegistration(_issued_at=_AT)  # type: ignore[call-arg]
