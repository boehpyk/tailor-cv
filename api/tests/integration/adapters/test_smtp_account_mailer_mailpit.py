"""AC-22: a real delivery through `SmtpAccountMailer` to Mailpit, read back over its HTTP API.

The fakes in `test_smtp_account_mailer.py` prove the adapter's decisions; this proves the bytes
survive a real SMTP server and arrive as a person would receive them. Source of truth: AC-22 and
AC-21 (subject, sender, recipient, a fragment link carrying the token, plain text only).

Endpoint: `MAILPIT_SMTP_HOST` / `MAILPIT_SMTP_PORT` / `MAILPIT_API_URL`, set only in CI (where the
service container is on the runner's loopback); locally they default to the compose service
(`mailpit`, 1025, `http://mailpit:8025`).

**A missing Mailpit fails, it never skips**: the house has no skip-if-service-absent precedent (the
database and Redis fixtures fail), and a skipped real-protocol test is a green run that proved
nothing, which is the exact failure this slice's CI service exists to prevent.

**Isolation**: every test mails a recipient unique to it, looks messages up by that address, and
deletes only the ids it found. Another developer looking at the same Mailpit loses nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest

from tailorcraft.domain.identity.account_mail import ConfirmYourEmail, ResetYourPassword
from tailorcraft.domain.identity.value_objects import EmailAddress
from tailorcraft.infrastructure.identity.one_time_tokens import (
    SecretsOneTimeTokenMinter,
    hash_presented,
)
from tailorcraft.infrastructure.mail.messages import (
    CONFIRM_PATH,
    RESET_PATH,
    SUBJECT_CONFIRM,
    SUBJECT_RESET,
)
from tailorcraft.infrastructure.mail.smtp import SmtpAccountMailer
from tailorcraft.infrastructure.settings import Settings

_API: Final = os.environ.get("MAILPIT_API_URL", "http://mailpit:8025")
_SMTP_HOST: Final = os.environ.get("MAILPIT_SMTP_HOST", "mailpit")
_SMTP_PORT: Final = int(os.environ.get("MAILPIT_SMTP_PORT", "1025"))
_BASE_URL: Final = "https://cv.example"
_FROM: Final = "no-reply@tailorcraft.test"


def _settings() -> Settings:
    return Settings(
        app_env="test",
        mail_smtp_host=_SMTP_HOST,
        mail_smtp_port=_SMTP_PORT,
        mail_smtp_security="none",
        mail_from_address=_FROM,
        mail_from_name="TailorCraft",
        public_base_url=_BASE_URL,
    )


@pytest.fixture
async def inbox() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=_API, timeout=10, trust_env=False) as client:
        try:
            (await client.get("/api/v1/info")).raise_for_status()
        except httpx.HTTPError as exc:
            pytest.fail(
                f"Mailpit is not reachable at {_API} ({type(exc).__name__}). The real-delivery "
                "test fails rather than skips: start it (`make up.dev`) or, in CI, check the "
                "mailpit service container."
            )
        yield client


@pytest.fixture
def recipient() -> str:
    return f"qa-{uuid.uuid4().hex}@example.org"


async def _messages_to(inbox: httpx.AsyncClient, address: str) -> list[dict[str, Any]]:
    response = await inbox.get("/api/v1/search", params={"query": f"to:{address}"})
    response.raise_for_status()
    messages: list[dict[str, Any]] = response.json()["messages"]
    return messages


async def _discard(inbox: httpx.AsyncClient, address: str) -> None:
    """Delete this test's messages only, by id, never the whole inbox."""
    ids = [m["ID"] for m in await _messages_to(inbox, address)]
    if ids:
        (await inbox.request("DELETE", "/api/v1/messages", json={"IDs": ids})).raise_for_status()


async def _one_message(inbox: httpx.AsyncClient, address: str) -> dict[str, Any]:
    found = await _messages_to(inbox, address)
    assert len(found) == 1, f"expected exactly one message to {address}, found {len(found)}"
    detail = await inbox.get(f"/api/v1/message/{found[0]['ID']}")
    detail.raise_for_status()
    body: dict[str, Any] = detail.json()
    return body


def _token_after(text: str, path: str) -> str:
    link = next(word for word in text.split() if word.startswith(_BASE_URL + path))
    return link.removeprefix(_BASE_URL + path)


async def test_a_confirmation_mail_is_delivered_with_the_ac21_headers_and_a_fragment_link(
    inbox: httpx.AsyncClient, recipient: str
) -> None:
    minted = SecretsOneTimeTokenMinter().mint()
    mail = ConfirmYourEmail(
        to=EmailAddress.parse(recipient), token=minted.token, expires_in=timedelta(hours=24)
    )
    try:
        await SmtpAccountMailer(_settings()).send(mail)

        message = await _one_message(inbox, recipient)

        assert message["Subject"] == SUBJECT_CONFIRM
        assert message["From"]["Address"] == _FROM
        assert [to["Address"] for to in message["To"]] == [recipient]
        presented = _token_after(message["Text"], CONFIRM_PATH)
        assert hash_presented(presented) == minted.token_hash
    finally:
        await _discard(inbox, recipient)


async def test_a_delivered_mail_is_plain_text_only(
    inbox: httpx.AsyncClient, recipient: str
) -> None:
    minted = SecretsOneTimeTokenMinter().mint()
    mail = ConfirmYourEmail(
        to=EmailAddress.parse(recipient), token=minted.token, expires_in=timedelta(hours=24)
    )
    try:
        await SmtpAccountMailer(_settings()).send(mail)

        message = await _one_message(inbox, recipient)
        raw = await inbox.get(f"/api/v1/message/{message['ID']}/raw")
        raw.raise_for_status()

        assert message["Text"].strip() != ""
        assert message["HTML"] == ""
        assert message["Attachments"] == []
        assert "text/html" not in raw.text.lower()
        assert "multipart" not in raw.text.lower()
    finally:
        await _discard(inbox, recipient)


async def test_a_reset_mail_carries_its_own_subject_and_a_reset_path_token(
    inbox: httpx.AsyncClient, recipient: str
) -> None:
    minted = SecretsOneTimeTokenMinter().mint()
    mail = ResetYourPassword(
        to=EmailAddress.parse(recipient), token=minted.token, expires_in=timedelta(hours=1)
    )
    try:
        await SmtpAccountMailer(_settings()).send(mail)

        message = await _one_message(inbox, recipient)

        assert message["Subject"] == SUBJECT_RESET
        assert hash_presented(_token_after(message["Text"], RESET_PATH)) == minted.token_hash
    finally:
        await _discard(inbox, recipient)


async def test_the_link_travels_in_the_fragment_and_never_in_a_query_string(
    inbox: httpx.AsyncClient, recipient: str
) -> None:
    minted = SecretsOneTimeTokenMinter().mint()
    mail = ConfirmYourEmail(
        to=EmailAddress.parse(recipient), token=minted.token, expires_in=timedelta(hours=24)
    )
    try:
        await SmtpAccountMailer(_settings()).send(mail)

        text = (await _one_message(inbox, recipient))["Text"]

        assert f"{_BASE_URL}{CONFIRM_PATH}{minted.token.reveal()}" in text
        assert "?token=" not in text
    finally:
        await _discard(inbox, recipient)


async def test_discarding_removes_this_tests_message_and_leaves_other_mail_alone(
    inbox: httpx.AsyncClient, recipient: str
) -> None:
    """The positive control for the cleanup: it is by recipient, so a bystander survives."""
    bystander = f"qa-bystander-{uuid.uuid4().hex}@example.org"
    mailer = SmtpAccountMailer(_settings())
    minter = SecretsOneTimeTokenMinter()
    try:
        for address in (recipient, bystander):
            await mailer.send(
                ConfirmYourEmail(
                    to=EmailAddress.parse(address),
                    token=minter.mint().token,
                    expires_in=timedelta(hours=24),
                )
            )

        await _discard(inbox, recipient)

        assert await _messages_to(inbox, recipient) == []
        assert len(await _messages_to(inbox, bystander)) == 1
    finally:
        await _discard(inbox, recipient)
        await _discard(inbox, bystander)
