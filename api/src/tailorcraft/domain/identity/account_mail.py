"""The mail an account is sent: which message, to whom, and what it carries (ADR-0026).

`AccountMail` is a **sum type** of three frozen dataclasses, `Owner`'s shape (ADR-0022): the mail
adapter `match`es on it with `assert_never` in the default arm, so a fourth message is a type error at
every `match` that forgot it.

**Content is not here.** Subject lines, wording and the link's URL are presentation, rendered by the
adapter (`infrastructure/mail/`); the domain says *which* message and *what it carries* — an address,
a `OneTimeToken`, and a lifetime the copy can state ("this link works for 24 hours").

**`repr` masks `to`, and that is this module's one rule.** `EmailAddress` prints itself (it is not a
secret in a request, where the person typed it), but a mail object is built in the worker, passed to
an adapter that talks to a network, and is exactly what a traceback frame, a failed `assert` or a
`%r` in a log line will hold. The token is masked by its own type (`OneTimeToken`); the address is
masked here, by each type's own `__repr__` (AC-5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TypeAlias

from tailorcraft.domain.identity.value_objects import EmailAddress, OneTimeToken


@dataclass(frozen=True, slots=True)
class ConfirmYourEmail:
    """A new sign-up's confirmation link (ADR-0027): the address registered, the token whose hash the
    pending registration holds, and how long the link lives.

    `expires_in` is relative, as `IssuedAccessToken.expires_in` is: the copy says "24 hours", never a
    clock time in a time zone the reader is not in.
    """

    to: EmailAddress
    token: OneTimeToken
    expires_in: timedelta

    def __post_init__(self) -> None:
        """Refuse `expires_in <= 0` (`InvariantViolated`) — a link that is dead on arrival is a bug
        in whoever computed the lifetime, not a mail worth sending.

        SKELETON: a no-op (AC-6). The masked `__repr__` lands with the GREEN; until then the
        generated one prints the address, which a masking assertion refuses on its own terms.
        """


@dataclass(frozen=True, slots=True)
class AccountAlreadyExists:
    """Sent instead of a confirmation link when the address already has an account (ADR-0027,
    technical plan §0.2): the worker — which nobody can time — decides which mail goes, so the
    register request has no branch. Carries no token: there is nothing to confirm, and the copy
    points at *Log in* and *Forgot your password?*."""

    to: EmailAddress


@dataclass(frozen=True, slots=True)
class ResetYourPassword:
    """A password-reset link (ADR-0028), sent only to an address that has an account."""

    to: EmailAddress
    token: OneTimeToken
    expires_in: timedelta

    def __post_init__(self) -> None:
        """Refuse `expires_in <= 0` (`InvariantViolated`), as `ConfirmYourEmail` does.

        SKELETON: a no-op (AC-6); the masked `__repr__` lands with the GREEN.
        """


# `TypeAlias` rather than a `type` statement for `ownership.py`'s reason: `isinstance` and class
# patterns must work on it at runtime.
AccountMail: TypeAlias = ConfirmYourEmail | AccountAlreadyExists | ResetYourPassword  # noqa: UP040
