"""Application tests for `RevokeAllLogins` (T13, RED).

Covers AC-13 against an **in-memory `LoginRepository` fake** (`tests/integration/fakes.py`): the
break-glass that signs everybody out, and its `dry_run` rehearsal that deletes nothing (OQ-5). No
clock, no events — the skeleton's own docstring says why (a mass deletion has no aggregate to load,
and an event per login would be a flood nobody chose).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tailorcraft.application.identity.revoke_all_logins import RevokeAllLogins
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.value_objects import TokenHash, UserId
from tests.integration.fakes import FakeLoginRepository

_LIFETIME = timedelta(days=30)
_T0 = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def _token_hash(seed: str) -> TokenHash:
    return TokenHash(value=hashlib.sha256(seed.encode()).hexdigest())


async def _seed_logins(logins: FakeLoginRepository, count: int) -> None:
    for i in range(count):
        login = Login.start(
            logins.next_identity(), UserId(value=uuid4()), _token_hash(f"rv{i}"), _T0, _LIFETIME
        )
        login.release_events()
        await logins.add(login)


async def test_dry_run_true_removes_nothing_and_returns_the_count() -> None:
    logins = FakeLoginRepository()
    await _seed_logins(logins, 3)
    use_case = RevokeAllLogins(logins)

    result = await use_case(dry_run=True)

    assert result == 3
    assert len(logins.all()) == 3
    assert logins.removed == []


async def test_dry_run_false_removes_every_login_and_returns_the_count() -> None:
    logins = FakeLoginRepository()
    await _seed_logins(logins, 4)
    use_case = RevokeAllLogins(logins)

    result = await use_case(dry_run=False)

    assert result == 4
    assert logins.all() == []


async def test_a_second_real_run_is_idempotent_and_returns_zero() -> None:
    logins = FakeLoginRepository()
    await _seed_logins(logins, 2)
    use_case = RevokeAllLogins(logins)

    first = await use_case(dry_run=False)
    second = await use_case(dry_run=False)

    assert first == 2
    assert second == 0
    assert logins.all() == []


async def test_on_an_empty_table_returns_zero_whichever_mode() -> None:
    logins = FakeLoginRepository()
    use_case = RevokeAllLogins(logins)

    assert await use_case(dry_run=True) == 0
    assert await use_case(dry_run=False) == 0
