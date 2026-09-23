"""`FailedLoginObserver` as one structlog line: `identity.login_failed` (I-9, I-10).

The port exists because `LogIn` must raise one attribute-less `InvalidCredentials` for both causes
(AC-9) while the log line must still say which it was — and `application/` does not log. So the use
case reports the cause here, immediately before it raises, and this adapter writes it down inside the
privacy tests' field of view (`domain/identity/ports.py` has the whole argument).

**What crosses is a reason and, for a wrong password, the id of an account that exists.** Never the
email, never a hash of it, never the password: an unknown email is somebody's address that is not an
account here, and "which addresses were tried" is not a list this service keeps.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Final

import structlog

from tailorcraft.domain.identity.value_objects import UserId

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import FailedLoginObserver

log = structlog.get_logger(__name__)

EVENT_LOGIN_FAILED: Final = "identity.login_failed"


class LoggingFailedLoginObserver:
    """Logs each refused login once, at `info` — a refused login is routine; the rate limiters are
    what answer a burst of them (I-14, I-15), and a warning per typo would drown the ones that matter.

    **Never raises** (the port's contract): a failure to *record* a refused login must not turn the
    401 into a 500. `contextlib.suppress(Exception)` around each call is that guarantee; `Exception`
    and not `BaseException`, so a cancellation still cancels.
    """

    def unknown_email(self) -> None:
        with contextlib.suppress(Exception):
            log.info(EVENT_LOGIN_FAILED, reason="unknown_email")

    def wrong_password(self, user_id: UserId) -> None:
        with contextlib.suppress(Exception):
            log.info(EVENT_LOGIN_FAILED, reason="wrong_password", user_id=str(user_id.value))


if TYPE_CHECKING:
    # Makes mypy prove the class structurally satisfies the port. Never executed.
    def _assert_implements_failed_login_observer(observer: LoggingFailedLoginObserver) -> None:
        _: FailedLoginObserver = observer
