"""Application tests for `LogOut` (T13, RED).

Covers AC-11 against an **in-memory `LoginRepository` fake** (`tests/integration/fakes.py`):
idempotence (`None`, an unknown hash, a current hash, a retired hash, and twice in a row), and that
`LoggedOut` is published only when something was actually removed. Database truths wait for T31.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tailorcraft.application.identity.log_out import LogOut
from tailorcraft.domain.identity.events import LoggedOut
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.value_objects import TokenHash, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import CountingClock, FakeLoginRepository, RecordingEventPublisher

_LIFETIME = timedelta(days=30)
_T0 = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def _token_hash(seed: str) -> TokenHash:
    return TokenHash(value=hashlib.sha256(seed.encode()).hexdigest())


async def _seed_login(logins: FakeLoginRepository, *, current_hash: TokenHash) -> Login:
    """A login "already in the database" — constructed directly, its seeding `LoggedIn` discarded
    (see `test_refresh_login.py`'s module docstring for why)."""
    login = Login.start(logins.next_identity(), UserId(value=uuid4()), current_hash, _T0, _LIFETIME)
    login.release_events()
    await logins.add(login)
    return login


async def test_presented_none_is_a_no_op(clock: FixedClock) -> None:
    """No refresh cookie at all — nothing removed, nothing published, no error."""
    logins = FakeLoginRepository()
    events = RecordingEventPublisher()
    use_case = LogOut(logins, CountingClock(clock), events)

    await use_case(None)  # returns None either way (`-> None`); nothing to assign

    assert logins.removed == []
    assert events.published == []


async def test_removes_the_login_by_its_current_hash_and_publishes_loggedout(
    clock: FixedClock,
) -> None:
    logins = FakeLoginRepository()
    h1 = _token_hash("o1")
    login = await _seed_login(logins, current_hash=h1)
    events = RecordingEventPublisher()
    counting_clock = CountingClock(clock)
    use_case = LogOut(logins, counting_clock, events)

    await use_case(h1)

    assert logins.removed == [login.id]
    assert events.published == [
        LoggedOut(user_id=login.user_id, login_id=login.id, occurred_at=clock.now())
    ]
    assert counting_clock.calls == 1


async def test_removes_the_login_by_a_retired_hash(clock: FixedClock) -> None:
    """A retired token logs the whole family out too (module docstring, `LogOut`'s own docstring):
    it is not judged for reuse, since the holder is asking for the login to end anyway."""
    logins = FakeLoginRepository()
    h1 = _token_hash("o2")
    login = await _seed_login(logins, current_hash=h1)
    h2 = _token_hash("o3")
    retired = login.rotate(h2, _T0)
    login.release_events()
    await logins.save_rotation(login, retired)
    events = RecordingEventPublisher()
    use_case = LogOut(logins, CountingClock(clock), events)

    await use_case(h1)  # the now-retired original hash

    assert logins.removed == [login.id]
    assert events.published == [
        LoggedOut(user_id=login.user_id, login_id=login.id, occurred_at=clock.now())
    ]


async def test_unknown_hash_is_a_no_op(clock: FixedClock) -> None:
    logins = FakeLoginRepository()
    await _seed_login(logins, current_hash=_token_hash("o4"))
    events = RecordingEventPublisher()
    use_case = LogOut(logins, CountingClock(clock), events)

    await use_case(_token_hash("o5"))  # matches nothing

    assert logins.removed == []
    assert events.published == []


async def test_called_twice_in_a_row_is_idempotent_and_not_an_error(clock: FixedClock) -> None:
    logins = FakeLoginRepository()
    h1 = _token_hash("o6")
    login = await _seed_login(logins, current_hash=h1)
    events = RecordingEventPublisher()
    use_case = LogOut(logins, CountingClock(clock), events)

    await use_case(h1)
    await use_case(h1)  # the login is already gone — must not raise

    assert logins.removed == [login.id]  # exactly once — the second call found nothing to remove
    assert len(events.published) == 1  # LoggedOut published only on the call that actually removed
