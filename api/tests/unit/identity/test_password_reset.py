"""AC-3: the `PasswordReset` aggregate — addressed, then issued (the address dropped), once.

Pure domain tests; against the skeleton every aggregate method raises `NotImplementedError`, the
expected red. Exact exception types throughout (`NotImplementedError` is a `RuntimeError`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.errors import (
    PasswordResetAlreadyIssued,
    PasswordResetExpired,
    PasswordResetNotIssued,
)
from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.value_objects import (
    AddressedReset,
    EmailAddress,
    IssuedReset,
    PasswordResetId,
    TokenHash,
    UserId,
)
from tailorcraft.domain.shared.errors import InvariantViolated

_ID = PasswordResetId(value=UUID("66666666-6666-7666-8666-666666666666"))
_USER_ID = UserId(value=UUID("44444444-4444-7444-8444-444444444444"))
_AT = datetime(2026, 10, 2, 10, 0, 0, tzinfo=UTC)
_TTL = timedelta(hours=1)
_TOKEN_HASH = TokenHash("ab" * 32)
_OTHER_TOKEN_HASH = TokenHash("cd" * 32)


def _email() -> EmailAddress:
    return EmailAddress.parse("alex@example.com")


def _requested(ttl: timedelta = _TTL) -> PasswordReset:
    return PasswordReset.request(_ID, _email(), _AT, ttl)


def _issued() -> PasswordReset:
    reset = _requested()
    reset.issue(_USER_ID, _TOKEN_HASH, _AT + timedelta(seconds=5))
    return reset


# --- the sum type ------------------------------------------------------------------------------


def test_the_two_targets_are_distinct_types_holding_one_field_each() -> None:
    addressed: object = AddressedReset(_email())
    assert addressed != IssuedReset(_USER_ID)
    assert AddressedReset(_email()).email == _email()
    assert IssuedReset(_USER_ID).user_id == _USER_ID


def test_the_targets_are_methodless_data() -> None:
    for cls in (AddressedReset, IssuedReset):
        public = {name for name in vars(cls) if not name.startswith("_")}
        assert public <= {"email", "user_id"}, f"{cls.__name__} grew behaviour: {public}"


# --- request -----------------------------------------------------------------------------------


def test_request_targets_the_address_and_is_not_issued() -> None:
    reset = _requested()

    assert reset.id == _ID
    assert reset.target == AddressedReset(_email())
    assert reset.requested_at == _AT
    assert reset.token_hash is None
    assert reset.issued_at is None


def test_request_sets_expires_at_to_the_instant_plus_the_ttl() -> None:
    assert _requested(timedelta(minutes=45)).expires_at == _AT + timedelta(minutes=45)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
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


# --- issue: the address is dropped -------------------------------------------------------------


def test_issue_retargets_to_the_account_and_drops_the_address() -> None:
    reset = _requested()

    reset.issue(_USER_ID, _TOKEN_HASH, _AT + timedelta(minutes=1))

    target: object = reset.target
    assert target == IssuedReset(_USER_ID)
    assert type(target) is IssuedReset  # and so no longer an AddressedReset


def test_issue_sets_the_token_hash_and_issued_at() -> None:
    reset = _requested()
    issued_at = _AT + timedelta(minutes=1)

    reset.issue(_USER_ID, _TOKEN_HASH, issued_at)

    assert reset.token_hash == _TOKEN_HASH
    assert reset.issued_at == issued_at


def test_an_issued_reset_exposes_its_user_id() -> None:
    assert _issued().user_id == _USER_ID


def test_an_addressed_reset_has_no_user_id() -> None:
    reset = _requested()

    with pytest.raises(PasswordResetNotIssued):
        _ = reset.user_id


def test_the_address_survives_nowhere_on_an_issued_reset() -> None:
    reset = _issued()

    assert "alex@example.com" not in repr(reset.target)
    assert "alex@example.com" not in {str(v) for v in vars(reset).values()}


def test_issue_refuses_an_expired_reset() -> None:
    with pytest.raises(PasswordResetExpired):
        _requested().issue(_USER_ID, _TOKEN_HASH, _AT + _TTL)  # inclusive


def test_a_refused_issue_leaves_the_reset_addressed_and_unissued() -> None:
    reset = _requested()

    with pytest.raises(PasswordResetExpired):
        reset.issue(_USER_ID, _TOKEN_HASH, _AT + _TTL)

    assert reset.target == AddressedReset(_email())
    assert reset.token_hash is None
    assert reset.issued_at is None


def test_issue_refuses_a_second_time() -> None:
    reset = _issued()

    with pytest.raises(PasswordResetAlreadyIssued):
        reset.issue(_USER_ID, _OTHER_TOKEN_HASH, _AT + timedelta(seconds=10))


def test_a_refused_second_issue_keeps_the_first_token_hash() -> None:
    reset = _issued()

    with pytest.raises(PasswordResetAlreadyIssued):
        reset.issue(_USER_ID, _OTHER_TOKEN_HASH, _AT + timedelta(seconds=10))

    assert reset.token_hash == _TOKEN_HASH


def test_issue_records_no_events() -> None:
    assert _issued().release_events() == ()


def test_the_mapped_attribute_names_are_not_a_constructor() -> None:
    with pytest.raises(TypeError):
        PasswordReset(_issued_at=_AT)  # type: ignore[call-arg]
