"""`SqlAlchemyExpiredIdentityTokens` and the committing wrappers around the one-time-token
repositories and the sweep (slice 2.5, T23, test-after; plan §0.9, AC-41's adapter half).

**Seeds live in the year 2001.** The sweep deletes *every* expired row in a table, so an absolute
count or an unscoped `DELETE` would also meet whatever any other test or run left committed. Every
instant here is far before any other test's, and `as_of` is chosen inside that era, so the only rows
`expires_at <= as_of` can match are this test's own (and the transaction is rolled back regardless).

**The commit discipline is counted on a real session**: `commit` is wrapped to count calls and still
delegates, so the wrappers run their real inner repositories against real PostgreSQL. A wrapper
that commits after a *read* or a *locking read* would release the lock the read exists to take; one
that commits when the inner write raised would be the `finally`-commit bug the module docstring warns
about.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for its side effect (`map_imperatively`); see test_pending_registration_repository.py.
import tailorcraft.infrastructure.persistence.retention.account_data  # noqa: F401
from tailorcraft.domain.identity.errors import (
    PasswordResetAlreadyIssued,
    PendingRegistrationAlreadyIssued,
)
from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    PasswordResetId,
    TokenHash,
    UserId,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identity.token_access import (
    CommittingPasswordResetRepository,
    CommittingPendingRegistrationRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.password_reset import (
    SqlAlchemyPasswordResetRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.pending_registration import (
    SqlAlchemyPendingRegistrationRepository,
)
from tailorcraft.infrastructure.persistence.retention.expired_identity_tokens import (
    SqlAlchemyExpiredIdentityTokens,
)
from tailorcraft.infrastructure.retention.data_access import CommittingExpiredIdentityTokens
from tests.integration.persistence.owner_rows import persist_user

_ERA = datetime(2001, 3, 4, 12, 0, 0, tzinfo=UTC)
_PHC = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"


# ------------------------------------------------------------------------------------- seeds


async def _pending(session: AsyncSession, expires_at: datetime) -> Any:
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_pending_registration (id, email, password_hash, requested_at, "
            "expires_at) VALUES (:i, :e, :p, :t, :x)"
        ),
        {
            "i": row_id,
            "e": f"{uuid4().hex}@example.com",
            "p": _PHC,
            "t": expires_at - timedelta(hours=24),
            "x": expires_at,
        },
    )
    return row_id


async def _reset(session: AsyncSession, expires_at: datetime) -> Any:
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_password_reset (id, email, requested_at, expires_at) "
            "VALUES (:i, :e, :t, :x)"
        ),
        {
            "i": row_id,
            "e": f"{uuid4().hex}@example.com",
            "t": expires_at - timedelta(hours=1),
            "x": expires_at,
        },
    )
    return row_id


async def _login(session: AsyncSession, user_id: UserId, expires_at: datetime) -> Any:
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_login (id, user_id, created_at, expires_at, generation, "
            "current_token_hash, version) VALUES (:i, :u, :t, :x, 1, :h, 1)"
        ),
        {
            "i": row_id,
            "u": user_id.value,
            "t": expires_at - timedelta(days=30),
            "x": expires_at,
            "h": uuid4().hex + uuid4().hex,
        },
    )
    return row_id


async def _exists(session: AsyncSession, table: str, row_id: Any) -> bool:
    assert table in {"identity_pending_registration", "identity_password_reset", "identity_login"}
    return bool(
        (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE id = :i"),  # noqa: S608 -- allow-listed
                {"i": row_id},
            )
        ).scalar_one()
    )


# ------------------------------------------------------------------------- the sweep adapter


async def test_a_row_expiring_exactly_at_as_of_is_deleted_and_one_second_later_is_kept(
    session: AsyncSession,
) -> None:
    """Inclusive `<=`: a row at exactly `expires_at` is already refused by every code path
    (`is_expired` is `at >= expires_at`), so it is already the sweep's."""
    sweep = SqlAlchemyExpiredIdentityTokens(session)
    at_boundary = await _pending(session, _ERA)
    just_after = await _pending(session, _ERA + timedelta(seconds=1))

    deleted = await sweep.delete_expired_pending(_ERA, 100)

    assert deleted == 1
    assert not await _exists(session, "identity_pending_registration", at_boundary)
    assert await _exists(session, "identity_pending_registration", just_after)


async def test_each_delete_touches_only_its_own_table(
    session: AsyncSession, clock: FixedClock
) -> None:
    sweep = SqlAlchemyExpiredIdentityTokens(session)
    owner = await persist_user(session, clock)
    pending = await _pending(session, _ERA)
    reset = await _reset(session, _ERA)
    login = await _login(session, owner.user_id, _ERA)

    assert await sweep.delete_expired_resets(_ERA, 100) == 1
    assert await _exists(session, "identity_pending_registration", pending)
    assert await _exists(session, "identity_login", login)
    assert not await _exists(session, "identity_password_reset", reset)

    assert await sweep.delete_expired_logins(_ERA, 100) == 1
    assert await _exists(session, "identity_pending_registration", pending)
    assert not await _exists(session, "identity_login", login)

    assert await sweep.delete_expired_pending(_ERA, 100) == 1
    assert not await _exists(session, "identity_pending_registration", pending)


@pytest.mark.parametrize(
    "method", ["delete_expired_pending", "delete_expired_resets", "delete_expired_logins"]
)
async def test_a_batch_is_bounded_by_limit_and_takes_the_oldest_first(
    session: AsyncSession, clock: FixedClock, method: str
) -> None:
    sweep = SqlAlchemyExpiredIdentityTokens(session)
    owner = await persist_user(session, clock)
    seed: Callable[[datetime], Awaitable[Any]]
    if method == "delete_expired_pending":
        table, seed = "identity_pending_registration", lambda at: _pending(session, at)
    elif method == "delete_expired_resets":
        table, seed = "identity_password_reset", lambda at: _reset(session, at)
    else:
        table, seed = "identity_login", lambda at: _login(session, owner.user_id, at)
    ids = [await seed(_ERA - timedelta(hours=hours)) for hours in (5, 4, 3, 2, 1)]  # oldest first

    deleted = await getattr(sweep, method)(_ERA, 2)

    assert deleted == 2
    assert [await _exists(session, table, row_id) for row_id in ids] == [
        False,
        False,
        True,
        True,
        True,
    ]
    assert await getattr(sweep, method)(_ERA, 2) == 2
    assert await getattr(sweep, method)(_ERA, 2) == 1
    assert await getattr(sweep, method)(_ERA, 2) == 0


async def test_deleting_an_expired_login_takes_its_retired_hashes(
    session: AsyncSession, clock: FixedClock
) -> None:
    sweep = SqlAlchemyExpiredIdentityTokens(session)
    owner = await persist_user(session, clock)
    login = await _login(session, owner.user_id, _ERA)
    await session.execute(
        text(
            "INSERT INTO identity_retired_refresh_token (token_hash, login_id, generation, "
            "retired_at) VALUES (:h, :l, 1, :t)"
        ),
        {"h": uuid4().hex + uuid4().hex, "l": login, "t": _ERA - timedelta(days=1)},
    )

    assert await sweep.delete_expired_logins(_ERA, 100) == 1

    left = (
        await session.execute(
            text("SELECT count(*) FROM identity_retired_refresh_token WHERE login_id = :l"),
            {"l": login},
        )
    ).scalar_one()
    assert left == 0


async def test_count_overdue_sums_the_three_tables_inclusively_at_the_grace_boundary(
    session: AsyncSession, clock: FixedClock
) -> None:
    sweep = SqlAlchemyExpiredIdentityTokens(session)
    owner = await persist_user(session, clock)
    grace = timedelta(hours=2)
    as_of = _ERA + grace  # cutoff = _ERA
    await _pending(session, _ERA)
    await _reset(session, _ERA - timedelta(minutes=1))
    await _login(session, owner.user_id, _ERA - timedelta(days=3))
    # not overdue: expired, but inside the grace
    await _pending(session, _ERA + timedelta(seconds=1))
    await _reset(session, _ERA + timedelta(hours=1))
    await _login(session, owner.user_id, as_of)

    assert await sweep.count_overdue(as_of, grace) == 3


async def test_count_overdue_deletes_nothing(session: AsyncSession) -> None:
    sweep = SqlAlchemyExpiredIdentityTokens(session)
    row = await _pending(session, _ERA)

    assert await sweep.count_overdue(_ERA + timedelta(hours=2), timedelta(hours=2)) == 1

    assert await _exists(session, "identity_pending_registration", row)


async def test_count_overdue_is_zero_when_nothing_is_overdue(session: AsyncSession) -> None:
    sweep = SqlAlchemyExpiredIdentityTokens(session)
    await _pending(session, _ERA + timedelta(days=1))

    assert await sweep.count_overdue(_ERA, timedelta(hours=2)) == 0


# ----------------------------------------------------------------------- the sweep's committing wrapper


class _CommitCounter:
    """Counts `commit()` calls on a real session while still delegating to it."""

    def __init__(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        original = session.commit

        async def _counting() -> None:
            self.count += 1
            await original()

        monkeypatch.setattr(session, "commit", _counting)


async def test_the_sweep_wrapper_commits_after_each_delete_and_not_after_the_count(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = _CommitCounter(session, monkeypatch)
    wrapped = CommittingExpiredIdentityTokens(SqlAlchemyExpiredIdentityTokens(session), session)
    await _pending(session, _ERA)

    await wrapped.count_overdue(_ERA + timedelta(hours=2), timedelta(hours=2))
    assert commits.count == 0

    await wrapped.delete_expired_pending(_ERA, 10)
    assert commits.count == 1
    await wrapped.delete_expired_resets(_ERA, 10)
    assert commits.count == 2
    await wrapped.delete_expired_logins(_ERA, 10)
    assert commits.count == 3


class _RaisingSweep:
    """An inner `ExpiredIdentityTokenPort` whose every write fails."""

    async def count_overdue(self, as_of: datetime, grace: timedelta) -> int:
        return 0

    async def delete_expired_pending(self, as_of: datetime, limit: int) -> int:
        raise RuntimeError("boom")

    async def delete_expired_resets(self, as_of: datetime, limit: int) -> int:
        raise RuntimeError("boom")

    async def delete_expired_logins(self, as_of: datetime, limit: int) -> int:
        raise RuntimeError("boom")


@pytest.mark.parametrize(
    "method", ["delete_expired_pending", "delete_expired_resets", "delete_expired_logins"]
)
async def test_the_sweep_wrapper_does_not_commit_when_the_inner_delete_raises(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    commits = _CommitCounter(session, monkeypatch)
    wrapped = CommittingExpiredIdentityTokens(_RaisingSweep(), session)

    with pytest.raises(RuntimeError):
        await getattr(wrapped, method)(_ERA, 10)

    assert commits.count == 0


# ------------------------------------------------------- the one-time-token repositories' wrappers


def _pending_aggregate(
    repo: SqlAlchemyPendingRegistrationRepository, clock: FixedClock
) -> PendingRegistration:
    return PendingRegistration.request(
        id=repo.next_identity(),
        email=EmailAddress.parse(f"{uuid4().hex}@example.com"),
        password_hash=PasswordHash(_PHC),
        at=clock.now(),
        ttl=timedelta(hours=24),
    )


async def test_the_pending_wrapper_commits_after_put_save_issued_and_remove_only(
    session: AsyncSession, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = _CommitCounter(session, monkeypatch)
    inner = SqlAlchemyPendingRegistrationRepository(session)
    wrapped = CommittingPendingRegistrationRepository(inner, session)
    pending = _pending_aggregate(inner, clock)

    await wrapped.put(pending)
    assert commits.count == 1

    await wrapped.get(pending.id)
    assert commits.count == 1, "a read must not commit"

    session.expunge_all()
    loaded = await inner.get(pending.id)
    assert loaded is not None
    loaded.issue(TokenHash("1" * 64), clock.now())
    await wrapped.save_issued(loaded)
    assert commits.count == 2

    assert await wrapped.lock_by_token_hash(TokenHash("1" * 64)) is not None
    assert commits.count == 2, "a locking read must not commit: that would release its lock"

    await wrapped.remove(pending.id)
    assert commits.count == 3


async def test_the_pending_wrapper_does_not_commit_a_save_issued_that_was_refused(
    session: AsyncSession, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = SqlAlchemyPendingRegistrationRepository(session)
    pending = _pending_aggregate(inner, clock)
    await inner.put(pending)
    session.expunge_all()
    stale = await inner.get(pending.id)
    assert stale is not None
    stale.issue(TokenHash("1" * 64), clock.now())
    await inner.remove(pending.id)  # the row is gone: the guarded UPDATE matches nothing
    commits = _CommitCounter(session, monkeypatch)
    wrapped = CommittingPendingRegistrationRepository(inner, session)

    with pytest.raises(PendingRegistrationAlreadyIssued):
        await wrapped.save_issued(stale)

    assert commits.count == 0


async def test_the_reset_wrapper_commits_after_add_save_issued_and_remove_only(
    session: AsyncSession, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = _CommitCounter(session, monkeypatch)
    owner = await persist_user(session, clock)
    inner = SqlAlchemyPasswordResetRepository(session)
    wrapped = CommittingPasswordResetRepository(inner, session)
    reset = PasswordReset.request(
        id=inner.next_identity(),
        email=EmailAddress.parse(f"{uuid4().hex}@example.com"),
        at=clock.now(),
        ttl=timedelta(hours=1),
    )

    await wrapped.add(reset)
    assert commits.count == 1

    await wrapped.get(reset.id)
    assert commits.count == 1

    session.expunge_all()
    loaded = await inner.get(reset.id)
    assert loaded is not None
    loaded.issue(owner.user_id, TokenHash("2" * 64), clock.now())
    await wrapped.save_issued(loaded)
    assert commits.count == 2

    assert await wrapped.find_by_token_hash(TokenHash("2" * 64)) is not None
    assert await wrapped.lock_by_token_hash(TokenHash("2" * 64)) is not None
    assert commits.count == 2, "neither read may commit"

    assert await wrapped.remove_all_for_user(owner.user_id) == 1
    assert commits.count == 2, (
        "remove_all_for_user runs inside ResetPassword's transaction: committing it alone would "
        "split it from the user's new hash"
    )

    await wrapped.remove(reset.id)
    assert commits.count == 3


async def test_the_reset_wrapper_does_not_commit_a_save_issued_that_was_refused(
    session: AsyncSession, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await persist_user(session, clock)
    inner = SqlAlchemyPasswordResetRepository(session)
    reset = PasswordReset.request(
        id=PasswordResetId(uuid4()),
        email=EmailAddress.parse(f"{uuid4().hex}@example.com"),
        at=clock.now(),
        ttl=timedelta(hours=1),
    )
    reset.issue(owner.user_id, TokenHash("3" * 64), clock.now())  # never persisted: no row to match
    commits = _CommitCounter(session, monkeypatch)
    wrapped = CommittingPasswordResetRepository(inner, session)

    with pytest.raises(PasswordResetAlreadyIssued):
        await wrapped.save_issued(reset)

    assert commits.count == 0
