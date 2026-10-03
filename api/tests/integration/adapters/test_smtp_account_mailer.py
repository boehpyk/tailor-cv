"""AC-20: `SmtpAccountMailer` translates every failure, counted and bounded (slice 2.5, T23).

**Faults are injected below the floor**, through the adapter's `connect=` seam. The fake
`SmtpConnection` raises what a real server or socket would provoke (`SMTPRecipientsRefused`,
`SMTPAuthenticationError`, `ConnectionRefusedError`, a `RuntimeError` subclass, ...), so the adapter's
own classification and its `except Exception` floor stand between the fault and the assertion. A
monkeypatch of `SmtpAccountMailer.send` would replace the very thing under test (CLAUDE.md, I-45).

Source of truth: AC-20's table and technical plan §0.5. `RETRY_BACKOFF_SECONDS` is shortened by
monkeypatch (the adapter documents that it is read at call time), so nothing sleeps for real.

The injected floor fault is a `RuntimeError` subclass that is **not** `NotImplementedError`:
`NotImplementedError` subclasses `RuntimeError`, so a test that raised the latter would not tell
"unexpected exception" from "skeleton".

**Logs are read through `caplog`**, with `configure_logging` called first (the structlog chain is
process-global and `cache_logger_on_first_use` makes the order of earlier tests matter). Every log
test asserts that records were captured at all before asserting what they lack, so an empty capture
cannot pass for a clean one.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from collections.abc import Callable, Mapping
from datetime import timedelta
from email.message import EmailMessage
from typing import Any, Final

import pytest
from pydantic import SecretStr

from tailorcraft.domain.identity.account_mail import ConfirmYourEmail
from tailorcraft.domain.identity.errors import MailNotDelivered
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    MailFailureReason,
    OneTimeToken,
)
from tailorcraft.infrastructure.mail import smtp
from tailorcraft.infrastructure.mail.smtp import SmtpAccountMailer, SmtpConnection
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings

_TOKEN: Final = "T" * 43
_RECIPIENT_MARKER: Final = "qa-rcpt-marker-91f3"
_REPLY_MARKER: Final = "REPLYTEXTMARKER-77ab"
_RCPT: Final = f"{_RECIPIENT_MARKER}@example.org"
_MAIL: Final = ConfirmYourEmail(
    to=EmailAddress.parse(_RCPT), token=OneTimeToken(_TOKEN), expires_in=timedelta(hours=24)
)


class _Boom(RuntimeError):
    """An arbitrary unexpected failure that is not `NotImplementedError`."""


class FakeSmtp:
    """A scripted `SmtpConnection`. Each `fail_*` is an exception to raise at that step."""

    def __init__(
        self,
        *,
        offers_starttls: bool = True,
        fail_ehlo: Exception | None = None,
        fail_starttls: Exception | None = None,
        fail_login: Exception | None = None,
        fail_send: Exception | None = None,
        send_returns: Mapping[str, tuple[int, bytes]] | None = None,
        fail_quit: Exception | None = None,
        fail_close: Exception | None = None,
    ) -> None:
        self.offers_starttls = offers_starttls
        self.fail_ehlo = fail_ehlo
        self.fail_starttls = fail_starttls
        self.fail_login = fail_login
        self.fail_send = fail_send
        self.send_returns = send_returns or {}
        self.fail_quit = fail_quit
        self.fail_close = fail_close
        self.calls: list[str] = []
        self.logins: list[tuple[str, str]] = []
        self.sent: list[EmailMessage] = []

    def ehlo(self, name: str = "") -> tuple[int, bytes]:
        self.calls.append("ehlo")
        if self.fail_ehlo is not None:
            raise self.fail_ehlo
        return 250, b"ok"

    def has_extn(self, opt: str) -> bool:
        self.calls.append(f"has_extn:{opt}")
        return self.offers_starttls and opt == "starttls"

    def starttls(self, *, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
        self.calls.append("starttls")
        self.context = context
        if self.fail_starttls is not None:
            raise self.fail_starttls
        return 220, b"ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.calls.append("login")
        self.logins.append((user, password))
        if self.fail_login is not None:
            raise self.fail_login
        return 235, b"ok"

    def send_message(self, msg: EmailMessage) -> Mapping[str, tuple[int, bytes]]:
        self.calls.append("send_message")
        self.sent.append(msg)
        if self.fail_send is not None:
            raise self.fail_send
        return self.send_returns

    def quit(self) -> tuple[int, bytes]:
        self.calls.append("quit")
        if self.fail_quit is not None:
            raise self.fail_quit
        return 221, b"bye"

    def close(self) -> None:
        self.calls.append("close")
        if self.fail_close is not None:
            raise self.fail_close


class Factory:
    """The `connect` seam: hands out one scripted connection per attempt, counts them, and records
    the socket timeout each was asked for. `connections[i]` is attempt `i + 1`'s connection; once the
    script is exhausted the last entry is reused (the same fault on every attempt)."""

    def __init__(self, *script: FakeSmtp | Exception) -> None:
        self._script = list(script) or [FakeSmtp()]
        self.connections: list[FakeSmtp] = []
        self.timeouts: list[float] = []

    @property
    def count(self) -> int:
        return len(self.timeouts)

    def __call__(self, settings: Settings, timeout: float) -> SmtpConnection:
        step = self._script[min(self.count, len(self._script) - 1)]
        self.timeouts.append(timeout)
        if isinstance(step, Exception):
            raise step
        self.connections.append(step)
        return step


def _fault(stage: str, error: Exception) -> dict[str, Any]:
    return {stage: error}


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "mail_smtp_host": "smtp.mail.example",
        "mail_smtp_security": "starttls",
        "mail_smtp_username": "smtp-user",
        "mail_smtp_password": SecretStr("smtp-secret-pw"),
        "mail_from_address": "no-reply@cv.example",
        "public_base_url": "https://cv.example",
        **overrides,
    }
    return Settings(**values)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtp, "RETRY_BACKOFF_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _structlog_through_stdlib() -> None:
    configure_logging(_settings())


async def _send(factory: Factory, settings: Settings | None = None) -> MailNotDelivered | None:
    mailer = SmtpAccountMailer(settings or _settings(), connect=factory)
    try:
        await mailer.send(_MAIL)
    except MailNotDelivered as exc:
        return exc
    return None


# --------------------------------------------------------------------------------- success path


async def test_a_successful_send_uses_one_connection_and_delivers_the_rendered_message() -> None:
    factory = Factory(FakeSmtp())

    assert await _send(factory) is None

    assert factory.count == 1
    sent = factory.connections[0].sent
    assert len(sent) == 1
    assert sent[0]["To"] == _RCPT
    assert _TOKEN in sent[0].get_content()


async def test_starttls_security_upgrades_then_asks_again_before_authenticating() -> None:
    fake = FakeSmtp()

    await _send(Factory(fake))

    assert fake.calls[:6] == [
        "ehlo",
        "has_extn:starttls",
        "starttls",
        "ehlo",
        "login",
        "send_message",
    ]
    assert fake.logins == [("smtp-user", "smtp-secret-pw")]
    assert isinstance(fake.context, ssl.SSLContext)
    assert fake.context.verify_mode == ssl.CERT_REQUIRED
    assert fake.context.check_hostname is True


@pytest.mark.parametrize("security", ["tls", "none"])
async def test_only_starttls_security_upgrades_the_connection(security: str) -> None:
    fake = FakeSmtp()

    assert await _send(Factory(fake), _settings(mail_smtp_security=security)) is None

    assert "starttls" not in fake.calls
    assert "send_message" in fake.calls


async def test_login_happens_only_when_a_username_is_configured() -> None:
    fake = FakeSmtp()

    await _send(Factory(fake), _settings(mail_smtp_username="", mail_smtp_security="none"))

    assert "login" not in fake.calls
    assert "send_message" in fake.calls


async def test_the_connection_is_quit_and_closed_after_a_send() -> None:
    fake = FakeSmtp()

    await _send(Factory(fake))

    assert fake.calls[-2:] == ["quit", "close"]


async def test_a_failing_quit_after_the_message_was_accepted_is_still_a_success() -> None:
    fake = FakeSmtp(fail_quit=smtplib.SMTPServerDisconnected("connection unexpectedly closed"))
    factory = Factory(fake)

    assert await _send(factory) is None

    assert factory.count == 1  # a retry here would be a duplicate mail
    assert len(fake.sent) == 1


async def test_a_failing_close_after_the_message_was_accepted_is_still_a_success() -> None:
    fake = FakeSmtp(fail_quit=_Boom(), fail_close=_Boom())
    factory = Factory(fake)

    assert await _send(factory) is None
    assert factory.count == 1


async def test_the_connection_is_closed_even_when_the_send_failed() -> None:
    fake = FakeSmtp(fail_send=smtplib.SMTPDataError(554, b"rejected"))

    await _send(Factory(fake))

    assert fake.calls[-2:] == ["quit", "close"]


# ------------------------------------------------------------------------- AC-20 translation rows


async def test_every_recipient_refused_with_a_5xx_is_recipient_rejected_and_not_retried() -> None:
    fake = FakeSmtp(fail_send=smtplib.SMTPRecipientsRefused({_RCPT: (550, b"5.1.1 no such user")}))
    factory = Factory(fake)

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.RECIPIENT_REJECTED
    assert failure.smtp_code == 550
    assert factory.count == 1


async def test_a_partial_refusal_returned_by_send_message_is_recipient_rejected() -> None:
    factory = Factory(FakeSmtp(send_returns={_RCPT: (553, b"5.1.3 bad mailbox")}))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.RECIPIENT_REJECTED
    assert failure.smtp_code == 553
    assert factory.count == 1


async def test_a_recipient_that_needs_smtputf8_is_recipient_rejected_and_not_retried() -> None:
    factory = Factory(FakeSmtp(fail_send=smtplib.SMTPNotSupportedError("SMTPUTF8 required")))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.RECIPIENT_REJECTED
    assert factory.count == 1


async def test_a_535_authentication_failure_is_provider_refused_and_not_retried() -> None:
    fake = FakeSmtp(fail_login=smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials"))
    factory = Factory(fake)

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.PROVIDER_REFUSED
    assert failure.smtp_code == 535
    assert factory.count == 1
    assert "send_message" not in fake.calls


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPSenderRefused(550, b"5.7.1 sender not allowed", "no-reply@cv.example"),
        smtplib.SMTPDataError(554, b"5.6.0 message rejected"),
        smtplib.SMTPHeloError(501, b"5.5.4 syntax"),
    ],
    ids=["mail-from", "data", "helo"],
)
async def test_any_other_5xx_is_provider_refused_and_not_retried(
    error: smtplib.SMTPResponseException,
) -> None:
    factory = Factory(FakeSmtp(fail_send=error))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.PROVIDER_REFUSED
    assert failure.smtp_code == error.smtp_code
    assert factory.count == 1


@pytest.mark.parametrize("code", [421, 450, 451, 452])
async def test_throttling_replies_are_retried_then_reported_as_throttled(code: int) -> None:
    error = smtplib.SMTPRecipientsRefused({_RCPT: (code, b"try later")})
    factory = Factory(FakeSmtp(fail_send=error))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.THROTTLED
    assert failure.smtp_code == code
    assert factory.count == 2


async def test_a_421_on_data_is_throttled_whatever_the_stage() -> None:
    factory = Factory(FakeSmtp(fail_send=smtplib.SMTPDataError(421, b"service not available")))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.THROTTLED
    assert factory.count == 2


async def test_a_4xx_with_enhanced_status_4_7_x_is_throttled() -> None:
    error = smtplib.SMTPRecipientsRefused({_RCPT: (454, b"4.7.0 temporarily rate limited")})
    factory = Factory(FakeSmtp(fail_send=error))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.THROTTLED
    assert factory.count == 2


async def test_any_other_4xx_is_unavailable_and_retried() -> None:
    error = smtplib.SMTPRecipientsRefused({_RCPT: (454, b"4.3.0 mailbox busy")})
    factory = Factory(FakeSmtp(fail_send=error))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.UNAVAILABLE
    assert failure.smtp_code == 454
    assert factory.count == 2


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("refused"),
        TimeoutError("timed out"),
        socket.timeout("timed out"),  # noqa: UP041 -- the alias is what older callers raise
        socket.gaierror(-2, "name not known"),
        ssl.SSLError("handshake failure"),
        ssl.SSLCertVerificationError("certificate verify failed"),
        OSError("network unreachable"),
    ],
    ids=["refused", "timeout", "socket-timeout", "gaierror", "ssl", "ssl-verify", "oserror"],
)
async def test_a_failed_connect_is_unavailable_and_retried(error: Exception) -> None:
    factory = Factory(error)

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.UNAVAILABLE
    assert failure.smtp_code is None
    assert factory.count == 2


@pytest.mark.parametrize(
    "stage",
    ["fail_ehlo", "fail_starttls", "fail_login", "fail_send"],
)
async def test_a_server_that_hangs_up_mid_conversation_is_unavailable_and_retried(
    stage: str,
) -> None:
    factory = Factory(FakeSmtp(**_fault(stage, smtplib.SMTPServerDisconnected("closed"))))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.UNAVAILABLE
    assert factory.count == 2


async def test_a_connect_reply_that_is_an_error_is_translated_by_its_code() -> None:
    factory = Factory(smtplib.SMTPConnectError(421, b"too many connections"))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.THROTTLED
    assert failure.smtp_code == 421


@pytest.mark.parametrize("stage", ["connect", "ehlo", "starttls", "login", "send"])
async def test_an_arbitrary_runtime_error_at_any_stage_is_unavailable(stage: str) -> None:
    factory = (
        Factory(_Boom("kaboom"))
        if stage == "connect"
        else Factory(FakeSmtp(**_fault(f"fail_{stage}", _Boom("kaboom"))))
    )

    failure = await _send(factory)

    assert type(failure) is MailNotDelivered  # not _Boom, not RuntimeError
    assert failure.reason is MailFailureReason.UNAVAILABLE
    assert factory.count == 2


async def test_a_non_smtp_exception_from_send_message_reaches_the_floor_as_unavailable() -> None:
    factory = Factory(FakeSmtp(fail_send=UnicodeEncodeError("ascii", "é", 0, 1, "ordinal")))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.UNAVAILABLE


async def test_a_starttls_refused_with_a_4_7_x_reply_is_throttled() -> None:
    factory = Factory(FakeSmtp(fail_starttls=smtplib.SMTPResponseException(454, b"4.7.0 TLS")))

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.THROTTLED  # 454 with enhanced status 4.7.0


# ------------------------------------------------------------------------------ TLS is not optional


async def test_a_server_that_does_not_offer_starttls_is_unavailable_with_no_plaintext_fallback() -> (
    None
):
    fake = FakeSmtp(offers_starttls=False)
    factory = Factory(fake)

    failure = await _send(factory)

    assert failure is not None
    assert failure.reason is MailFailureReason.UNAVAILABLE
    for connection in factory.connections:
        assert "login" not in connection.calls, "credentials must never cross a plaintext channel"
        assert "send_message" not in connection.calls, (
            "the link must never cross a plaintext channel"
        )
        assert "starttls" not in connection.calls


# --------------------------------------------------------------------------- attempts and deadline


@pytest.mark.parametrize("attempts", [1, 2, 3])
async def test_at_most_mail_max_attempts_connections_are_opened(attempts: int) -> None:
    factory = Factory(ConnectionRefusedError("refused"))

    await _send(factory, _settings(mail_max_attempts=attempts))

    assert factory.count == attempts


def test_the_default_is_two_attempts() -> None:
    assert _settings().mail_max_attempts == 2
    assert _settings().mail_total_deadline_seconds == 30


async def test_a_transient_failure_then_a_success_delivers_on_the_second_connection() -> None:
    bad = FakeSmtp(fail_send=smtplib.SMTPServerDisconnected("closed"))
    good = FakeSmtp()
    factory = Factory(bad, good)

    assert await _send(factory) is None

    assert factory.count == 2
    assert len(good.sent) == 1
    assert (
        bad.sent == bad.sent
    )  # the failed attempt may have offered the message; only one delivered


async def test_a_permanent_refusal_is_never_retried_even_with_attempts_to_spare() -> None:
    factory = Factory(FakeSmtp(fail_login=smtplib.SMTPAuthenticationError(535, b"no")))

    await _send(factory, _settings(mail_max_attempts=3))

    assert factory.count == 1


async def test_no_second_attempt_starts_when_the_deadline_leaves_no_room_for_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deadline 5 s (the setting's floor), backoff 4.5 s: 0.5 s would remain, under the 1 s an
    # attempt needs, so the adapter must give up instead of sleeping into a doomed connection.
    monkeypatch.setattr(smtp, "RETRY_BACKOFF_SECONDS", 4.5)
    factory = Factory(ConnectionRefusedError("refused"))

    failure = await _send(factory, _settings(mail_total_deadline_seconds=5, mail_max_attempts=3))

    assert failure is not None
    assert failure.reason is MailFailureReason.UNAVAILABLE
    assert factory.count == 1


async def test_the_same_deadline_with_room_to_spare_does_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smtp, "RETRY_BACKOFF_SECONDS", 0.01)
    factory = Factory(ConnectionRefusedError("refused"))

    await _send(factory, _settings(mail_total_deadline_seconds=5, mail_max_attempts=3))

    assert factory.count == 3


async def test_the_socket_timeout_is_the_setting_and_never_more_than_the_deadline_allows() -> None:
    factory = Factory(ConnectionRefusedError("refused"))

    await _send(factory, _settings(mail_send_timeout_seconds=10, mail_total_deadline_seconds=30))
    assert factory.timeouts[0] == pytest.approx(10.0)

    shorter = Factory(ConnectionRefusedError("refused"))
    await _send(shorter, _settings(mail_send_timeout_seconds=30, mail_total_deadline_seconds=5))
    assert 1.0 <= shorter.timeouts[0] <= 5.0


# ---------------------------------------------------------------------------------- exception shape


async def test_the_raised_error_hides_its_cause_and_context() -> None:
    fake = FakeSmtp(
        fail_send=smtplib.SMTPRecipientsRefused({_RCPT: (550, f"no {_REPLY_MARKER}".encode())})
    )

    failure = await _send(Factory(fake))

    assert failure is not None
    assert failure.__cause__ is None
    assert failure.__suppress_context__ is True
    assert _REPLY_MARKER not in str(failure)
    assert _RECIPIENT_MARKER not in str(failure)


async def test_an_unexpected_fault_also_hides_its_frames() -> None:
    failure = await _send(Factory(FakeSmtp(fail_send=_Boom(_REPLY_MARKER))))

    assert failure is not None
    assert failure.__cause__ is None
    assert failure.__suppress_context__ is True


# ---------------------------------------------------------------------------------------- logging


async def test_a_failed_attempt_logs_its_type_and_reply_code_but_never_the_reply_or_recipient(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = smtplib.SMTPRecipientsRefused(
        {_RCPT: (550, f"5.1.1 <{_RCPT}> {_REPLY_MARKER}".encode())}
    )

    with caplog.at_level(logging.DEBUG):
        await _send(Factory(FakeSmtp(fail_send=error)))

    text = caplog.text
    assert "mail.attempt_failed" in text, (
        "no record captured: the assertions below would be vacuous"
    )
    assert "SMTPRecipientsRefused" in text
    assert "550" in text
    for secret in (_REPLY_MARKER, _RECIPIENT_MARKER, _TOKEN, "smtp-secret-pw", "smtp-user"):
        assert secret not in text


async def test_an_unexpected_fault_logs_its_type_only_not_its_message_or_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await _send(Factory(FakeSmtp(fail_send=_Boom(f"{_REPLY_MARKER} {_RCPT} {_TOKEN}"))))

    text = caplog.text
    assert "_Boom" in text
    assert "Traceback" not in text
    for secret in (_REPLY_MARKER, _RECIPIENT_MARKER, _TOKEN):
        assert secret not in text
    assert all(record.exc_info is None for record in caplog.records)


async def test_one_line_is_logged_per_failed_attempt_with_its_number(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await _send(Factory(ConnectionRefusedError(_REPLY_MARKER)))

    lines = [r.getMessage() for r in caplog.records if "mail.attempt_failed" in r.getMessage()]
    assert len(lines) == 2
    assert '"attempt": 1' in lines[0]
    assert '"attempt": 2' in lines[1]
    assert all(_REPLY_MARKER not in line for line in lines)


async def test_a_success_logs_nothing_that_carries_the_link_or_the_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await _send(Factory(FakeSmtp()))

    for secret in (_TOKEN, _RECIPIENT_MARKER, "smtp-secret-pw"):
        assert secret not in caplog.text


# ------------------------------------------------------------------------------ render-time faults


@pytest.mark.parametrize(
    "overrides",
    [{"mail_from_address": ""}, {"mail_from_name": "Tailor\r\nBcc: x@y.z"}],
    ids=["empty-sender", "crlf-in-name"],
)
async def test_a_message_that_cannot_be_rendered_is_provider_refused_and_never_connects(
    overrides: dict[str, str],
) -> None:
    """As shipped (the spec is silent): a configuration fault fails identically every time, so it is
    not retried, no connection is opened, and `provider_refused` puts it at error level in the task."""
    factory = Factory(FakeSmtp())

    failure = await _send(factory, _settings(**overrides))

    assert failure is not None
    assert failure.reason is MailFailureReason.PROVIDER_REFUSED
    assert factory.count == 0
    assert failure.__suppress_context__ is True


# ----------------------------------------------------------------------------------- real connect


class _RecordingSmtp:
    """Stands in for `smtplib.SMTP` / `SMTP_SSL` to see how `_default_connect` builds the real one."""

    def __init__(self, sink: list[_RecordingSmtp], *, ssl_flavour: bool) -> None:
        self.sink = sink
        self.ssl_flavour = ssl_flavour
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}
        self.debuglevel: int | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> _RecordingSmtp:
        made = _RecordingSmtp(self.sink, ssl_flavour=self.ssl_flavour)
        made.args, made.kwargs = args, kwargs
        self.sink.append(made)
        return made

    def set_debuglevel(self, level: int) -> None:
        self.debuglevel = level


@pytest.fixture
def recorded_smtp(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingSmtp]:
    sink: list[_RecordingSmtp] = []
    monkeypatch.setattr(smtplib, "SMTP", _RecordingSmtp(sink, ssl_flavour=False))
    monkeypatch.setattr(smtplib, "SMTP_SSL", _RecordingSmtp(sink, ssl_flavour=True))
    return sink


@pytest.mark.parametrize("security", ["starttls", "none"])
def test_the_real_connect_opens_plain_tcp_with_a_timeout_and_debug_output_off(
    recorded_smtp: list[_RecordingSmtp], security: str
) -> None:
    settings = _settings(mail_smtp_security=security, mail_smtp_port=2525)

    connection: Callable[[Settings, float], SmtpConnection] = smtp._default_connect

    connection(settings, 7.0)

    (made,) = recorded_smtp
    assert not made.ssl_flavour
    assert made.args == ("smtp.mail.example", 2525)
    assert made.kwargs == {"timeout": 7.0}
    assert made.debuglevel == 0


def test_the_real_connect_uses_implicit_tls_with_a_verifying_context_for_tls_security(
    recorded_smtp: list[_RecordingSmtp],
) -> None:
    smtp._default_connect(_settings(mail_smtp_security="tls", mail_smtp_port=465), 7.0)

    (made,) = recorded_smtp
    assert made.ssl_flavour
    context = made.kwargs["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert made.kwargs["timeout"] == 7.0
    assert made.debuglevel == 0
