"""The mail half of a valid production `Settings` (slice 2.5, AC-24).

`APP_ENV=production` refuses an empty SMTP host, `none` security, empty credentials, an unparseable
sender and a non-https base URL. A test that builds a production `Settings` to prove a **different**
refusal (or a success) spreads this in, so it stays about the thing it names and never fails for a
mail reason it did not mean to test. Fake values only, none of them a real host or credential.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import SecretStr


class ProductionMail(TypedDict):
    mail_smtp_host: str
    mail_smtp_security: Literal["starttls", "tls", "none"]
    mail_smtp_username: str
    mail_smtp_password: SecretStr
    mail_from_address: str
    public_base_url: str


PRODUCTION_MAIL: ProductionMail = {
    "mail_smtp_host": "smtp.mail.example",
    "mail_smtp_security": "starttls",
    "mail_smtp_username": "smtp-user",
    "mail_smtp_password": SecretStr("smtp-password-for-tests"),
    "mail_from_address": "no-reply@cv.example",
    "public_base_url": "https://cv.example",
}
