"""`AccountMailPort` over SMTP submission, on the standard library (slice 2.5, ADR-0026, plan §0.5).

**Not a guarded egress** (ADR-0012 is for a host the *caller* chooses). The host here is a setting
read once, no request can influence it, and there is no redirect to follow. The obligations that
still apply are applied: a socket timeout on every operation, a total deadline, TLS verified by
`ssl.create_default_context()`, STARTTLS **required** unless the security is `none` (a server that
does not offer it is `unavailable`, never a plaintext fallback), and an `except Exception` floor.

**Blocking I/O, so a thread.** `smtplib` is synchronous, and every attempt (connect, EHLO, STARTTLS,
AUTH, MAIL/RCPT/DATA, QUIT) runs in one `asyncio.to_thread`. `wait_for` cannot cancel a thread (1.5's
lesson), so the bound that holds is the **socket's** timeout, `MAIL_SEND_TIMEOUT_SECONDS`, shrunk to
what is left of the deadline when less is left. Celery's hard limit is the outer bound.

**One retry mechanism, here** (ADR-0014 §6). At most `MAIL_MAX_ATTEMPTS` connections, `unavailable`
and `throttled` only, `RETRY_BACKOFF_SECONDS` apart, and no attempt starts once
`MAIL_TOTAL_DEADLINE_SECONDS` leaves too little room for one. The task declares no retry and no
`countdown`.

**The translation table is AC-20's**, classified inside the thread where the stage is known (a
`SMTPNotSupportedError` while sending is a recipient that needs SMTPUTF8; while upgrading it is a
server without STARTTLS):

| Failure | Reason |
|---|---|
| `SMTPRecipientsRefused`, 5xx on RCPT | `recipient_rejected` |
| `SMTPAuthenticationError` (535) | `provider_refused` |
| any reply 421/450/451/452, or a 4xx with enhanced status `4.7.x` | `throttled` (retried) |
| any other 4xx reply | `unavailable` (retried) |
| any other 5xx reply (`MAIL FROM`, `DATA`, `HELO`) | `provider_refused` |
| connect refused, timeout, `ssl.SSLError`, `SMTPServerDisconnected`, any `OSError` | `unavailable` |
| STARTTLS not offered, any other `SMTPException` | `unavailable` |
| anything else (the floor) | `unavailable` |

**What is logged, and what never is.** One `mail.attempt_failed` line per failed attempt, carrying
the attempt number, the exception's **type** and the reply **code**. Never the reply's text (a 550
quotes the recipient), never the recipient, never the body (it holds the link, which is a
credential), and never `exc_info`. `MailNotDelivered` is raised `from None`, so no frame holding the
message or the server's reply is reachable from a Sentry report. `debuglevel` is pinned 0: smtplib's
debug output prints every line of the conversation, body included, to stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import smtplib
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from tailorcraft.domain.identity.account_mail import AccountMail
from tailorcraft.domain.identity.errors import MailNotDelivered
from tailorcraft.domain.identity.value_objects import MailFailureReason
from tailorcraft.infrastructure.mail.messages import render
from tailorcraft.infrastructure.settings import Settings

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import AccountMailPort

log = structlog.get_logger(__name__)

# Between two attempts. Read at call time, so a test can shorten it without a new constructor seam.
RETRY_BACKOFF_SECONDS: float = 2.0

# No new attempt starts with less than this left before the deadline: a connection given half a
# second cannot complete a TLS handshake, so starting one only adds a failure line.
_MIN_ATTEMPT_SECONDS: Final = 1.0

_EVENT_ATTEMPT_FAILED: Final = "mail.attempt_failed"
_EVENT_RENDER_REFUSED: Final = "mail.render_refused"

# Replies AC-20 names as throttling, whatever the stage.
_THROTTLE_CODES: Final = frozenset({421, 450, 451, 452})
_RETRYABLE: Final = frozenset({MailFailureReason.UNAVAILABLE, MailFailureReason.THROTTLED})


class SmtpConnection(Protocol):
    """The part of `smtplib.SMTP` this adapter drives. The `connect` seam returns one, already
    connected; a test returns a fake whose methods raise what a real server would provoke, so a
    fault lands **below** the adapter's floor rather than replacing it."""

    def ehlo(self, name: str = ...) -> tuple[int, bytes]: ...

    def has_extn(self, opt: str) -> bool: ...

    def starttls(self, *, context: ssl.SSLContext | None = ...) -> tuple[int, bytes]: ...

    def login(self, user: str, password: str) -> tuple[int, bytes]: ...

    def send_message(self, msg: EmailMessage) -> Mapping[str, tuple[int, bytes]]: ...

    def quit(self) -> tuple[int, bytes]: ...

    def close(self) -> None: ...


# `(settings, timeout_seconds) -> a connected SmtpConnection`. Called inside the attempt's thread.
Connect = Callable[[Settings, float], SmtpConnection]


def _default_connect(settings: Settings, timeout: float) -> SmtpConnection:
    """Open the real connection: implicit TLS for `tls`, plain TCP for `starttls` and `none` (the
    upgrade happens in `_attempt`, where a missing STARTTLS is refused). `debuglevel` is pinned 0."""
    connection: smtplib.SMTP
    if settings.mail_smtp_security == "tls":
        connection = smtplib.SMTP_SSL(
            settings.mail_smtp_host,
            settings.mail_smtp_port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = smtplib.SMTP(settings.mail_smtp_host, settings.mail_smtp_port, timeout=timeout)
    connection.set_debuglevel(0)
    return connection


class StartTlsNotOffered(smtplib.SMTPException):
    """The server's EHLO did not advertise STARTTLS and the security is `starttls`. Never a
    plaintext fallback: the message carries a credential."""


@dataclass(frozen=True, slots=True)
class _Failure:
    """One attempt's classified failure. Only what may be logged: a reason, a code, a type name."""

    reason: MailFailureReason
    smtp_code: int | None
    error_type: str


class SmtpAccountMailer:
    """Send account mail through the configured SMTP submission server.

    `connect` is the testing seam, a constructor argument whose default is the real connection
    (ADR-0012's rule for seams: strict by default, nothing in `src/` passes another).
    """

    def __init__(self, settings: Settings, connect: Connect = _default_connect) -> None:
        self._settings = settings
        self._connect = connect

    async def send(self, mail: AccountMail) -> None:
        """Deliver `mail`, or raise `MailNotDelivered(reason, smtp_code)` after the bounded retry."""
        try:
            message = render(mail, self._settings)
        except Exception as exc:
            # A message that cannot be rendered is a configuration fault (an empty or broken
            # `MAIL_FROM_ADDRESS`, a CR/LF in `MAIL_FROM_NAME`): it will fail the same way every
            # time, so it is not retried, and `provider_refused` puts it at error level in the task's
            # line, where an operator will see it.
            log.error(_EVENT_RENDER_REFUSED, error_type=type(exc).__name__)
            raise MailNotDelivered(MailFailureReason.PROVIDER_REFUSED) from None

        failure = await self._deliver(message)
        if failure is None:
            return
        # `from None`: the server's reply and the message are in the frames this suppresses.
        raise MailNotDelivered(failure.reason, failure.smtp_code) from None

    async def _deliver(self, message: EmailMessage) -> _Failure | None:
        """Up to `MAIL_MAX_ATTEMPTS` attempts inside the deadline. `None` once one succeeds,
        otherwise the last attempt's failure."""
        settings = self._settings
        attempts = max(1, settings.mail_max_attempts)
        deadline = time.monotonic() + settings.mail_total_deadline_seconds
        attempt = 0
        while True:
            attempt += 1
            # The socket timeout of this attempt: the setting, or what is left of the deadline when
            # that is less. Never below the floor, so a near-spent deadline cannot make the socket
            # non-blocking (`timeout=0`).
            remaining = deadline - time.monotonic()
            timeout = max(
                _MIN_ATTEMPT_SECONDS, min(float(settings.mail_send_timeout_seconds), remaining)
            )
            try:
                failure = await asyncio.to_thread(self._attempt, message, timeout)
            except Exception as exc:
                # The floor outside the thread: `_attempt` classifies everything it can see, so this
                # is the thread hop itself failing. `Exception`, never `BaseException`: a cancelled
                # task must still cancel.
                failure = _Failure(MailFailureReason.UNAVAILABLE, None, type(exc).__name__)
            if failure is None:
                return None
            log.warning(
                _EVENT_ATTEMPT_FAILED,
                attempt=attempt,
                error_type=failure.error_type,
                smtp_code=failure.smtp_code,
            )
            if failure.reason not in _RETRYABLE or attempt >= attempts:
                return failure
            backoff = RETRY_BACKOFF_SECONDS
            if deadline - time.monotonic() - backoff < _MIN_ATTEMPT_SECONDS:
                # No room for another attempt inside the deadline: give up now rather than start
                # one that cannot finish.
                return failure
            await asyncio.sleep(backoff)

    def _attempt(self, message: EmailMessage, timeout: float) -> _Failure | None:
        """One connection, start to finish, in the worker thread. `None` on success, otherwise the
        classified failure. Never raises: everything is translated here, where the stage is known."""
        sending = False
        connection: SmtpConnection | None = None
        try:
            connection = self._connect(self._settings, timeout)
            connection.ehlo()
            if self._settings.mail_smtp_security == "starttls":
                if not connection.has_extn("starttls"):
                    raise StartTlsNotOffered
                connection.starttls(context=ssl.create_default_context())
                # RFC 3207: the server forgets the pre-TLS EHLO; ask again over the secure channel.
                connection.ehlo()
            username = self._settings.mail_smtp_username
            if username:
                connection.login(username, self._settings.mail_smtp_password.get_secret_value())
            sending = True
            refused = connection.send_message(message)
            if refused:
                # One recipient per message, so a partial refusal is a refusal of that recipient.
                # smtplib raises `SMTPRecipientsRefused` when *every* recipient is refused; this is
                # the shape it returns when some were accepted, kept total for a future caller.
                return _recipient_failure(refused, "SMTPRecipientsRefused")
        except Exception as exc:
            return _classify(exc, sending=sending)
        finally:
            if connection is not None:
                _close_quietly(connection)
        return None


def _close_quietly(connection: SmtpConnection) -> None:
    """QUIT, then close the socket. **Failures here are not failures of the send**: once DATA was
    answered 250 the message is the server's, and reporting a failed QUIT as `unavailable` would
    retry, which is a duplicate mail."""
    with contextlib.suppress(Exception):
        connection.quit()
    with contextlib.suppress(Exception):
        connection.close()


def _classify(exc: Exception, *, sending: bool) -> _Failure:
    """AC-20's table (see the module docstring). Reads codes; never reads or keeps a reply's text
    beyond the enhanced-status prefix it tests."""
    error_type = type(exc).__name__
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return _recipient_failure(exc.recipients, error_type)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return _Failure(MailFailureReason.PROVIDER_REFUSED, _code(exc.smtp_code), error_type)
    if isinstance(exc, smtplib.SMTPNotSupportedError) and sending:
        # V-26: a non-ASCII address offered to a server without SMTPUTF8. The recipient cannot be
        # reached through this server, whatever we retry.
        return _Failure(MailFailureReason.RECIPIENT_REJECTED, None, error_type)
    if isinstance(exc, smtplib.SMTPResponseException):
        # `SMTPSenderRefused`, `SMTPDataError`, `SMTPConnectError`, `SMTPHeloError`.
        code = _code(exc.smtp_code)
        return _Failure(_by_reply(code, exc.smtp_error, permanent=_PROVIDER), code, error_type)
    # `SMTPServerDisconnected`, `StartTlsNotOffered`, any other `SMTPException`; every `OSError`
    # (`ConnectionRefusedError`, `TimeoutError`, `socket.gaierror`, `ssl.SSLError`); and the floor.
    return _Failure(MailFailureReason.UNAVAILABLE, None, error_type)


_PROVIDER: Final = MailFailureReason.PROVIDER_REFUSED
_RECIPIENT: Final = MailFailureReason.RECIPIENT_REJECTED


def _recipient_failure(refused: Mapping[str, tuple[int, bytes]], error_type: str) -> _Failure:
    """A refusal at RCPT. The mapping is keyed by the address, which is never read: only the one
    value's code and enhanced status."""
    reply = next(iter(refused.values()), None)
    if reply is None:
        return _Failure(_RECIPIENT, None, error_type)
    raw_code, text = reply
    code = _code(raw_code)
    return _Failure(_by_reply(code, text, permanent=_RECIPIENT), code, error_type)


def _by_reply(
    code: int | None, text: bytes | str, *, permanent: MailFailureReason
) -> MailFailureReason:
    """Throttling (421/450/451/452, or a 4xx with enhanced status `4.7.x`), any other transient
    reply, or the stage's permanent reason for a 5xx. A reply with no usable code is `unavailable`:
    it did not get through, and nothing says it never will."""
    if code is None or not 400 <= code < 600:
        return MailFailureReason.UNAVAILABLE
    if code in _THROTTLE_CODES:
        return MailFailureReason.THROTTLED
    if code < 500:
        return MailFailureReason.THROTTLED if _enhanced_4_7(text) else MailFailureReason.UNAVAILABLE
    return permanent


def _enhanced_4_7(text: bytes | str) -> bool:
    prefix = text[:4] if isinstance(text, str) else text[:4].decode("ascii", errors="replace")
    return prefix == "4.7."


def _code(value: object) -> int | None:
    """A reply code worth logging: a three-digit integer, or `None` (smtplib uses `-1` for "no
    reply")."""
    return value if isinstance(value, int) and 100 <= value <= 599 else None


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_account_mail_port(mailer: SmtpAccountMailer) -> None:
        _: AccountMailPort = mailer
