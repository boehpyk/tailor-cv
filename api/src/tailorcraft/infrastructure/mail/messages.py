"""Render an `AccountMail` into the message that goes on the wire (slice 2.5, ADR-0026, AC-21).

The domain says *which* message and *what it carries*; this module owns everything a reader sees:
subject, wording, the link and the headers. Plain text only. An HTML part would add a sanitizer, a
second copy of every sentence and a way for a link's text and its target to disagree, and buys
nothing for three short notices.

**The token goes in the fragment** (`/confirm-email#token=…`), never in a query string. A browser
never sends the fragment to a server: not to nginx's access log, not to Traefik's, not in a
`Referer`. The page reads it from `location.hash` and POSTs it. A query string would put a live
credential in three logs before the user had clicked anything.

**Header values are checked for CR and LF before they are set** (`HeaderValueRefused`). Python's
`email` package also refuses them today; the explicit check makes the refusal ours, named, and
independent of a library's version. Every value here is either a constant, a validated
`EmailAddress` (ASCII, no control characters) or a setting, so the check guards configuration.

**Nothing here logs**, and nothing here can: the function returns a message and raises a type.
"""

from __future__ import annotations

from datetime import timedelta
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Final, assert_never

from tailorcraft.domain.identity.account_mail import (
    AccountAlreadyExists,
    AccountMail,
    ConfirmYourEmail,
    ResetYourPassword,
)
from tailorcraft.infrastructure.settings import Settings

SUBJECT_CONFIRM: Final = "Confirm your email address for TailorCraft"
SUBJECT_ACCOUNT_EXISTS: Final = "You already have a TailorCraft account"
SUBJECT_RESET: Final = "Reset your TailorCraft password"

# The paths the React app serves for each link (plan §7). The token rides in the fragment.
CONFIRM_PATH: Final = "/confirm-email#token="
RESET_PATH: Final = "/reset-password/confirm#token="
LOGIN_PATH: Final = "/login"
RESET_REQUEST_PATH: Final = "/reset-password"


class HeaderValueRefused(ValueError):
    """A header value held a CR or LF, or the sender is not configured. Carries the header's
    **name** only, never its value (the value may be an address)."""

    def __init__(self, header: str) -> None:
        super().__init__(f"refused a value for the {header} header")
        self.header = header


def render(mail: AccountMail, settings: Settings) -> EmailMessage:
    """Build the message for `mail`: headers per AC-21, a plain-text UTF-8 body, no HTML part.

    Raises:
        HeaderValueRefused: a header value carries CR/LF, or `MAIL_FROM_ADDRESS` is empty or has
            no domain (there is nothing to put in `From` or to scope the `Message-ID` to).
    """
    match mail:
        case ConfirmYourEmail(to=to, token=token, expires_in=expires_in):
            subject = SUBJECT_CONFIRM
            body = _confirm_body(_link(settings, CONFIRM_PATH) + token.reveal(), expires_in)
        case AccountAlreadyExists(to=to):
            subject = SUBJECT_ACCOUNT_EXISTS
            body = _account_exists_body(
                _link(settings, LOGIN_PATH), _link(settings, RESET_REQUEST_PATH)
            )
        case ResetYourPassword(to=to, token=token, expires_in=expires_in):
            subject = SUBJECT_RESET
            body = _reset_body(_link(settings, RESET_PATH) + token.reveal(), expires_in)
        case _:
            assert_never(mail)

    sender_address = _checked("From", settings.mail_from_address)
    _, at, sender_domain = sender_address.rpartition("@")
    if not at or not sender_domain:
        raise HeaderValueRefused("From")

    # `policy.SMTP`: `policy.default` with CRLF line endings, which is what the wire wants and what
    # `send_message` would otherwise have to convert.
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = formataddr((_checked("From", settings.mail_from_name), sender_address))
    message["To"] = _checked("To", to.value)
    message["Subject"] = _checked("Subject", subject)
    message["Date"] = formatdate(usegmt=True)
    # On the sender's domain, so a receiving server sees an id that belongs to the `From`.
    message["Message-ID"] = make_msgid(domain=sender_domain)
    # RFC 3834: an automatic message. Well-behaved auto-responders do not answer it, so a vacation
    # reply cannot bounce back and forth with the system that sent a link.
    message["Auto-Submitted"] = "auto-generated"
    # `7bit` when the body is ASCII (it always is today), so the link travels as written: under the
    # library's default, any line over 78 characters, which a link is, turns the whole body into
    # quoted-printable, and the link into `token=3D…` with a soft break in the middle. Readers decode
    # that, but a person reading the raw message or a test grepping it should not have to.
    message.set_content(body, charset="utf-8", cte="7bit" if body.isascii() else "quoted-printable")
    return message


def _link(settings: Settings, path: str) -> str:
    """`PUBLIC_BASE_URL` plus a path. A trailing slash on the base is dropped, so the link never
    carries `//`, which a browser treats as a different path."""
    return settings.public_base_url.rstrip("/") + path


def _checked(header: str, value: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise HeaderValueRefused(header)
    return value


def _confirm_body(link: str, expires_in: timedelta) -> str:
    return (
        "Hello,\n"
        "\n"
        "Someone, hopefully you, asked to create a TailorCraft account with this email address.\n"
        "To confirm it, open this link:\n"
        "\n"
        f"{link}\n"
        "\n"
        f"The link works once and for {_in_words(expires_in)}. Confirming does not sign you in: "
        "log in afterwards with the password you chose.\n"
        "\n"
        "If this was not you, ignore this message. No account is created unless the link is "
        "opened.\n"
        "\n"
        "TailorCraft\n"
    )


def _account_exists_body(login_link: str, reset_link: str) -> str:
    # OQ-13: no token. There is nothing to confirm, and a link that signed anyone in would make this
    # notice a credential sent to whoever typed the address.
    return (
        "Hello,\n"
        "\n"
        "Someone, hopefully you, tried to create a TailorCraft account with this email address. "
        "An account with this address already exists, so nothing was changed.\n"
        "\n"
        "To log in:\n"
        f"{login_link}\n"
        "\n"
        "If you have forgotten your password, you can reset it here:\n"
        f"{reset_link}\n"
        "\n"
        "If this was not you, ignore this message.\n"
        "\n"
        "TailorCraft\n"
    )


def _reset_body(link: str, expires_in: timedelta) -> str:
    return (
        "Hello,\n"
        "\n"
        "Someone, hopefully you, asked to reset the password of the TailorCraft account with this "
        "email address. To choose a new password, open this link:\n"
        "\n"
        f"{link}\n"
        "\n"
        f"The link works once and for {_in_words(expires_in)}. Choosing a new password signs you "
        "out everywhere.\n"
        "\n"
        "If this was not you, ignore this message. Your password stays as it is.\n"
        "\n"
        "TailorCraft\n"
    )


def _in_words(remaining: timedelta) -> str:
    """A lifetime in words: `24 hours`, `1 hour`, `45 minutes`, `1 hour and 30 minutes`.

    The worker computes `expires_at - now`, so the value is rarely round (`23:59:58`). Rounded to
    the nearest minute first, so two seconds of queueing never reads as "23 hours and 59 minutes".
    Relative on purpose: a clock time would be in a time zone the reader is not in.
    """
    minutes = max(1, round(remaining.total_seconds() / 60))
    hours, rest = divmod(minutes, 60)
    if hours == 0:
        return _plural(rest, "minute")
    if rest == 0:
        return _plural(hours, "hour")
    return f"{_plural(hours, 'hour')} and {_plural(rest, 'minute')}"


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"
