"""Adapter test for `ReuseAlertingEventPublisher` (`infrastructure/identity/reuse_alert.py`),
written against `/verify` round 1's finding 4, **before** the fix — a RED test against believed-buggy
behaviour, not a skeleton (the adapter has existed since slice 2.1's implementation; this is the
first test written for it at all).

The module's own docstring claims: *"The warning never raises. It runs inside the request whose
response must commit the revocation... A failure to record the alarm is strictly better than that."*
Read narrowly, that sentence is about the `log.warning` call alone, and it is true today —
`contextlib.suppress(Exception)` wraps exactly that line. But `/verify` reads the class's guarantee
more broadly than its own docstring states it: a wrapper built to sit between `RefreshLogin` and the
real event bus, specifically so a failure to *record the alarm* cannot cost the revocation, has no
business propagating a failure from *delegating the event* either — the caller
(`RefreshLogin.refresh`, technical plan §2) is exactly as exposed to an unhandled exception from
`await self._inner.publish(*events)` as it would be to one from the warning line, and the commit that
must survive this call does not care which half of `publish` threw.

No fakes elsewhere in this module: `EventPublisherPort` is a `typing.Protocol`, so a minimal
hand-written double is the whole fixture. Pure — no database, no event loop fixture beyond the one
pytest-asyncio itself provides, no Redis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from tailorcraft.domain.identity.events import RefreshTokenReuseDetected, UserRegistered
from tailorcraft.domain.identity.value_objects import LoginId, UserId
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.infrastructure.identity.reuse_alert import ReuseAlertingEventPublisher

AT = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
USER_ID = UserId(UUID("aabbccdd-eeff-1234-5678-90abcdef1234"))
LOGIN_ID = LoginId(UUID("11223344-5566-7788-99aa-bbccddeeff00"))


class _RaisingEventPublisher:
    """An `EventPublisherPort` double whose `publish` always fails — the fault is injected here,
    strictly *below* `ReuseAlertingEventPublisher`'s own floor, exactly as CLAUDE.md's "inject a
    fault below the floor you are testing" rule asks: this is the real collaborator the class wraps,
    not a monkeypatch of the class's own method, which would replace the very floor under test."""

    async def publish(self, *events: DomainEvent) -> None:
        raise RuntimeError("boom: the real event bus is unreachable")


async def test_a_publish_failure_in_the_inner_publisher_does_not_propagate() -> None:
    """RefreshTokenReuseDetected is the event this wrapper exists for — the case the module
    docstring is actually about. If `_inner.publish` raises, that must not escape `wrapper.publish`.

    RED today: nothing wraps `await self._inner.publish(*events)` in
    `ReuseAlertingEventPublisher.publish` (only the `log.warning` call below it is behind
    `contextlib.suppress(Exception)`), so the `RuntimeError` propagates straight out and this test
    is expected to fail with that exception, not with a clean `pytest.fail`.
    """
    wrapper = ReuseAlertingEventPublisher(_RaisingEventPublisher())
    event = RefreshTokenReuseDetected(
        occurred_at=AT,
        user_id=USER_ID,
        login_id=LOGIN_ID,
        generation_presented=3,
        generation_current=5,
    )

    try:
        await wrapper.publish(event)
    except Exception as exc:  # the whole point: must nothing escape, of any type?
        pytest.fail(
            f"ReuseAlertingEventPublisher.publish propagated {exc!r} from the inner publisher; "
            "the class's own docstring claims it never raises."
        )


async def test_a_publish_failure_in_the_inner_publisher_does_not_propagate_for_an_ordinary_event() -> (
    None
):
    """The delegate call (`await self._inner.publish(*events)`) runs for *every* event, not only a
    `RefreshTokenReuseDetected` — the alarm's `log.warning` is the special case, filtered inside the
    loop. So an inner failure on an everyday event (`UserRegistered`, carrying no alarm at all) must
    be swallowed exactly the same way; the guarantee cannot depend on which event triggered it.
    """
    wrapper = ReuseAlertingEventPublisher(_RaisingEventPublisher())
    event = UserRegistered(occurred_at=AT, user_id=USER_ID)

    try:
        await wrapper.publish(event)
    except Exception as exc:
        pytest.fail(
            f"ReuseAlertingEventPublisher.publish propagated {exc!r} from the inner publisher on an "
            "event with no alarm at all — the delegate call itself is unguarded."
        )
