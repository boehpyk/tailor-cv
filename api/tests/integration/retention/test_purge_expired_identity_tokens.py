"""Application tests for `PurgeExpiredIdentityTokens` (slice 2.5, T13 RED — AC-15, V-57).

Retention's sweep of identity rows that have aged out: pending registrations, password resets and
logins. The port is an in-memory `FakeExpiredIdentityTokens` holding `expires_at` instants, which
honours the port's contract (at most `limit` per call, `expires_at <= as_of` inclusive), so what is
asserted is the use case's own behaviour: it **drains** a backlog larger than one batch, passes the
instant and the batch size through unchanged, never asks for an unexpired row to go (the fake would
delete exactly what it is told is due, so unexpired survivors prove the `as_of` was not inflated), and
returns three counts. **It does not log** (the counts are returned; the task logs).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tailorcraft.application.retention.purge_expired_identity_tokens import (
    PurgeExpiredIdentityTokens,
)
from tailorcraft.domain.retention.value_objects import IdentityTokenSweepReport
from tests.integration.fakes import FakeExpiredIdentityTokens

_AS_OF = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
_BATCH = 3


def _ago(seconds: int) -> datetime:
    return _AS_OF - timedelta(seconds=seconds)


def _ahead(seconds: int) -> datetime:
    return _AS_OF + timedelta(seconds=seconds)


async def test_every_expired_row_of_each_kind_is_deleted_across_batches_and_counted() -> None:
    """A backlog (7 / 2 / 4) bigger than one batch of 3 is drained, not truncated."""
    port = FakeExpiredIdentityTokens(
        pending=[_ago(n) for n in range(1, 8)],
        resets=[_ago(n) for n in range(1, 3)],
        logins=[_ago(n) for n in range(1, 5)],
    )

    report = await PurgeExpiredIdentityTokens(port, _BATCH)(_AS_OF)

    assert report == IdentityTokenSweepReport(pending_registrations=7, password_resets=2, logins=4)
    assert (port.pending, port.resets, port.logins) == ([], [], [])


async def test_nothing_unexpired_is_deleted() -> None:
    """The unexpired rows survive and are not counted."""
    port = FakeExpiredIdentityTokens(
        pending=[_ago(5), _ahead(5), _ahead(3600)],
        resets=[_ahead(1)],
        logins=[_ago(1), _ahead(86400)],
    )

    report = await PurgeExpiredIdentityTokens(port, _BATCH)(_AS_OF)

    assert report == IdentityTokenSweepReport(pending_registrations=1, password_resets=0, logins=1)
    assert port.pending == [_ahead(5), _ahead(3600)]
    assert port.resets == [_ahead(1)]
    assert port.logins == [_ahead(86400)]


async def test_a_row_expiring_at_exactly_the_sweep_instant_is_deleted() -> None:
    """Inclusive, as every aggregate's `is_expired` is."""
    port = FakeExpiredIdentityTokens(pending=[_AS_OF], resets=[_AS_OF], logins=[_AS_OF])

    report = await PurgeExpiredIdentityTokens(port, _BATCH)(_AS_OF)

    assert report == IdentityTokenSweepReport(pending_registrations=1, password_resets=1, logins=1)


async def test_an_empty_store_returns_three_zeros() -> None:
    report = await PurgeExpiredIdentityTokens(FakeExpiredIdentityTokens(), _BATCH)(_AS_OF)

    assert report == IdentityTokenSweepReport(pending_registrations=0, password_resets=0, logins=0)


async def test_every_port_call_carries_the_callers_instant_and_the_configured_batch_size() -> None:
    """Bounded batches: no statement may be asked for more than `batch_size`, and the instant is the
    caller's — the use case reads no clock of its own."""
    port = FakeExpiredIdentityTokens(
        pending=[_ago(n) for n in range(1, 8)], resets=[_ago(1)], logins=[_ago(1)]
    )

    await PurgeExpiredIdentityTokens(port, _BATCH)(_AS_OF)

    assert port.calls  # something was asked
    assert {(as_of, limit) for _, as_of, limit, _ in port.calls} == {(_AS_OF, _BATCH)}
    assert {kind for kind, *_ in port.calls} == {"pending", "resets", "logins"}
    assert max(deleted for *_, deleted in port.calls) <= _BATCH


async def test_a_port_failure_propagates_exactly_and_what_already_went_stays_deleted() -> None:
    """V-57: the task fails and the next tick retries; each batch is durable on return."""

    class _DatabaseDown(Exception):
        pass

    failure = _DatabaseDown()
    port = FakeExpiredIdentityTokens(
        pending=[_ago(n) for n in range(1, 8)],
        fail_with=failure,
        fail_after=1,
    )

    with pytest.raises(_DatabaseDown) as raised:
        await PurgeExpiredIdentityTokens(port, _BATCH)(_AS_OF)

    assert raised.value is failure
    assert len(port.pending) == 4  # the first batch of 3 had already been deleted
