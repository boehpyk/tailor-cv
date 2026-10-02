"""AC-21: the message that goes on the wire, parsed back from its bytes (slice 2.5, T23, test-after).

Source of truth is AC-21 and technical plan §3, not `render`'s body. Every assertion reads the
**serialised** message (`as_bytes()` re-parsed with the standard library), because that is what a
mail server and a mail client receive: asserting on the `EmailMessage` object would test the
library's in-memory representation, which can differ from the wire (folding, transfer encoding).

Pure: no database, no network, no clock (the `Date` header is the one place `render` reads the
system clock; the test checks it parses and is recent rather than pinning a value).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Final

import pytest

from tailorcraft.domain.identity.account_mail import (
    AccountAlreadyExists,
    AccountMail,
    ConfirmYourEmail,
    ResetYourPassword,
)
from tailorcraft.domain.identity.value_objects import EmailAddress, OneTimeToken
from tailorcraft.infrastructure.mail.messages import HeaderValueRefused, render
from tailorcraft.infrastructure.settings import Settings

_TOKEN: Final = "Abc123_-" * 5 + "xyZ"  # 43 URL-safe characters
_TO: Final = EmailAddress.parse("alex.smith@example.org")
_BASE: Final = "https://cv.example"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "mail_from_address": "no-reply@cv.example",
        "mail_from_name": "TailorCraft",
        "public_base_url": _BASE,
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


def _wire(message: EmailMessage) -> EmailMessage:
    parsed = message_from_bytes(message.as_bytes(), policy=policy.default)
    assert isinstance(parsed, EmailMessage)
    return parsed


def _body(parsed: EmailMessage) -> str:
    part = parsed.get_body(preferencelist=("plain",))
    assert part is not None
    content = part.get_content()
    assert isinstance(content, str)
    return content


_CONFIRM = ConfirmYourEmail(to=_TO, token=OneTimeToken(_TOKEN), expires_in=timedelta(hours=24))
_RESET = ResetYourPassword(to=_TO, token=OneTimeToken(_TOKEN), expires_in=timedelta(hours=1))
_EXISTS = AccountAlreadyExists(to=_TO)
_ALL: list[AccountMail] = [_CONFIRM, _RESET, _EXISTS]


@pytest.mark.parametrize("mail", _ALL, ids=["confirm", "reset", "exists"])
def test_every_message_carries_the_required_headers(mail: AccountMail) -> None:
    parsed = _wire(render(mail, _settings()))

    assert parseaddr(str(parsed["From"])) == ("TailorCraft", "no-reply@cv.example")
    assert parsed["From"] == "TailorCraft <no-reply@cv.example>"
    assert parsed["To"] == "alex.smith@example.org"
    assert parsed["Auto-Submitted"] == "auto-generated"
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_content_charset() == "utf-8"
    sent_at = parsedate_to_datetime(str(parsed["Date"]))
    assert abs(datetime.now(UTC) - sent_at) < timedelta(minutes=5)


@pytest.mark.parametrize("mail", _ALL, ids=["confirm", "reset", "exists"])
def test_the_message_id_is_on_the_senders_domain(mail: AccountMail) -> None:
    parsed = _wire(render(mail, _settings()))

    assert re.fullmatch(r"<[^<>@\s]+@cv\.example>", str(parsed["Message-ID"]))


@pytest.mark.parametrize(
    ("mail", "subject"),
    [
        (_CONFIRM, "Confirm your email address for TailorCraft"),
        (_RESET, "Reset your TailorCraft password"),
        (_EXISTS, "You already have a TailorCraft account"),
    ],
    ids=["confirm", "reset", "exists"],
)
def test_each_message_has_its_own_subject(mail: AccountMail, subject: str) -> None:
    assert _wire(render(mail, _settings()))["Subject"] == subject


@pytest.mark.parametrize("mail", _ALL, ids=["confirm", "reset", "exists"])
def test_there_is_no_html_part_and_no_attachment(mail: AccountMail) -> None:
    parsed = _wire(render(mail, _settings()))

    assert not parsed.is_multipart()
    assert [part.get_content_type() for part in parsed.walk()] == ["text/plain"]
    assert "<html" not in parsed.as_string().lower()


def test_the_confirmation_link_is_the_base_url_plus_fragment_exactly() -> None:
    raw = render(_CONFIRM, _settings()).as_bytes().decode("ascii")

    assert f"{_BASE}/confirm-email#token={_TOKEN}\r\n" in raw


def test_the_reset_link_is_the_base_url_plus_fragment_exactly() -> None:
    raw = render(_RESET, _settings()).as_bytes().decode("ascii")

    assert f"{_BASE}/reset-password/confirm#token={_TOKEN}\r\n" in raw


@pytest.mark.parametrize("mail", [_CONFIRM, _RESET], ids=["confirm", "reset"])
def test_the_token_is_never_in_a_query_string(mail: AccountMail) -> None:
    body = _body(_wire(render(mail, _settings())))

    assert "?token" not in body
    assert "&token" not in body
    assert body.count(_TOKEN) == 1
    assert f"#token={_TOKEN}" in body


@pytest.mark.parametrize("mail", [_CONFIRM, _RESET], ids=["confirm", "reset"])
def test_the_link_travels_unencoded_so_the_raw_message_holds_it_verbatim(mail: AccountMail) -> None:
    message = render(mail, _settings())

    assert message["Content-Transfer-Encoding"] == "7bit"
    assert "=3D" not in message.as_bytes().decode("ascii")


def test_a_trailing_slash_on_the_base_url_does_not_double_the_slash() -> None:
    body = _body(_wire(render(_CONFIRM, _settings(public_base_url=f"{_BASE}/"))))

    assert f"{_BASE}/confirm-email#token={_TOKEN}" in body
    assert f"{_BASE}//" not in body


def test_the_confirmation_states_its_lifetime_in_words() -> None:
    body = _body(_wire(render(_CONFIRM, _settings())))

    assert "24 hours" in body


def test_the_reset_states_its_lifetime_in_words() -> None:
    body = _body(_wire(render(_RESET, _settings())))

    assert "1 hour" in body
    assert "1 hours" not in body


@pytest.mark.parametrize(
    ("lifetime", "words"),
    [
        (timedelta(hours=23, minutes=59, seconds=58), "24 hours"),
        (timedelta(minutes=45), "45 minutes"),
        (timedelta(minutes=90), "1 hour and 30 minutes"),
    ],
)
def test_a_lifetime_with_queueing_seconds_is_rounded_to_the_minute(
    lifetime: timedelta, words: str
) -> None:
    mail = ConfirmYourEmail(to=_TO, token=OneTimeToken(_TOKEN), expires_in=lifetime)

    assert words in _body(_wire(render(mail, _settings())))


def test_the_account_exists_notice_has_no_token_and_links_login_and_reset() -> None:
    body = _body(_wire(render(_EXISTS, _settings())))

    assert "token" not in body.lower()
    assert "#" not in body
    assert f"{_BASE}/login" in body
    assert f"{_BASE}/reset-password" in body
    assert "/confirm" not in body


@pytest.mark.parametrize("mail", _ALL, ids=["confirm", "reset", "exists"])
def test_a_cr_or_lf_in_the_sender_name_is_refused_before_sending(mail: AccountMail) -> None:
    for bad in ("Tailor\r\nBcc: victim@example.net", "Tailor\nCraft", "Tailor\rCraft"):
        with pytest.raises(HeaderValueRefused) as refused:
            render(mail, _settings(mail_from_name=bad))
        assert refused.value.header == "From"
        assert "victim" not in str(refused.value)


@pytest.mark.parametrize("sender", ["", "no-reply@", "no-reply"])
def test_an_unusable_sender_address_is_refused(sender: str) -> None:
    with pytest.raises(HeaderValueRefused):
        render(_CONFIRM, _settings(mail_from_address=sender))


def test_a_cr_or_lf_in_the_sender_address_is_refused() -> None:
    with pytest.raises(HeaderValueRefused):
        render(_CONFIRM, _settings(mail_from_address="a@cv.example\r\nBcc: x@y.z"))
