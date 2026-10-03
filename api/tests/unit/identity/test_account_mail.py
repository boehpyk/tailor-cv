"""AC-5: the mail types' masked `repr`, the `AccountMail` sum, and `MailNotDelivered`'s attributes.

Pure domain tests. Each masking test is a **positive pair**: the `repr` must contain the class name
(so the assertion cannot pass on an empty string) and must contain neither the address nor the
token plaintext. The token half already holds against the skeleton (`OneTimeToken` prints
`OneTimeToken()`); the address half is red, because `EmailAddress` prints itself.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from tailorcraft.domain.identity.account_mail import (
    AccountAlreadyExists,
    AccountMail,
    ConfirmYourEmail,
    ResetYourPassword,
)
from tailorcraft.domain.identity.errors import MailNotDelivered
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    MailFailureReason,
    OneTimeToken,
)

_ADDRESS = "alex.recipient@example.com"
_PLAINTEXT = "T" * 20 + "o" * 10 + "k" * 13  # 43 characters, recognisable in a repr
_TTL = timedelta(hours=24)


def _to() -> EmailAddress:
    return EmailAddress.parse(_ADDRESS)


def _token() -> OneTimeToken:
    return OneTimeToken(_PLAINTEXT)


def _mails() -> list[AccountMail]:
    return [
        ConfirmYourEmail(to=_to(), token=_token(), expires_in=_TTL),
        AccountAlreadyExists(to=_to()),
        ResetYourPassword(to=_to(), token=_token(), expires_in=timedelta(hours=1)),
    ]


@pytest.mark.parametrize("mail", _mails(), ids=lambda m: type(m).__name__)
def test_repr_names_the_class_but_never_the_address_or_the_token(mail: AccountMail) -> None:
    text = repr(mail)

    assert type(mail).__name__ in text
    assert _ADDRESS not in text
    assert "alex.recipient" not in text
    assert _PLAINTEXT not in text


@pytest.mark.parametrize("mail", _mails(), ids=lambda m: type(m).__name__)
def test_str_and_format_never_carry_the_address_or_the_token(mail: AccountMail) -> None:
    for text in (str(mail), f"{mail}", f"{mail!r}", "%s" % (mail,)):  # noqa: UP031
        assert type(mail).__name__ in text
        assert _ADDRESS not in text
        assert _PLAINTEXT not in text


def test_a_mail_that_carries_a_token_shows_it_only_through_its_mask() -> None:
    assert "OneTimeToken(***)" in repr(ConfirmYourEmail(to=_to(), token=_token(), expires_in=_TTL))
    assert "OneTimeToken(***)" in repr(ResetYourPassword(to=_to(), token=_token(), expires_in=_TTL))


def test_the_mail_types_keep_their_fields_readable_for_the_adapter() -> None:
    confirm = ConfirmYourEmail(to=_to(), token=_token(), expires_in=_TTL)

    assert confirm.to == _to()
    assert confirm.token.reveal() == _PLAINTEXT
    assert confirm.expires_in == _TTL
    assert AccountAlreadyExists(to=_to()).to == _to()


def test_account_already_exists_carries_no_token() -> None:
    assert {f.name for f in dataclasses.fields(AccountAlreadyExists)} == {"to"}


def test_the_mail_types_are_frozen() -> None:
    for mail in _mails():
        with pytest.raises(dataclasses.FrozenInstanceError):
            mail.to = _to()  # type: ignore[misc]


def test_the_field_sets_are_exactly_the_agreed_ones() -> None:
    assert [f.name for f in dataclasses.fields(ConfirmYourEmail)] == ["to", "token", "expires_in"]
    assert [f.name for f in dataclasses.fields(ResetYourPassword)] == ["to", "token", "expires_in"]


def test_account_mail_is_the_union_of_exactly_the_three_types() -> None:
    assert set(AccountMail.__args__) == {
        ConfirmYourEmail,
        AccountAlreadyExists,
        ResetYourPassword,
    }
    for mail in _mails():
        assert isinstance(mail, AccountMail)


# --- MailNotDelivered / MailFailureReason ------------------------------------------------------


def test_mail_failure_reason_is_exactly_the_four_agreed_values() -> None:
    assert {r.value for r in MailFailureReason} == {
        "recipient_rejected",
        "unavailable",
        "throttled",
        "provider_refused",
    }


def test_mail_not_delivered_carries_a_reason_and_an_smtp_code() -> None:
    error = MailNotDelivered(MailFailureReason.RECIPIENT_REJECTED, 550)

    assert error.reason is MailFailureReason.RECIPIENT_REJECTED
    assert error.smtp_code == 550


def test_mail_not_delivered_smtp_code_defaults_to_none() -> None:
    assert MailNotDelivered(MailFailureReason.UNAVAILABLE).smtp_code is None


def test_mail_not_delivered_has_no_attribute_but_reason_and_smtp_code() -> None:
    error = MailNotDelivered(MailFailureReason.THROTTLED, 421)

    assert set(vars(error)) == {"reason", "smtp_code"}


def test_mail_not_delivered_message_names_the_reason_and_nothing_else() -> None:
    error = MailNotDelivered(MailFailureReason.PROVIDER_REFUSED, 535)

    assert "provider_refused" in str(error)
