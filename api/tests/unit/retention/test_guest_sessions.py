"""Slice 4.4 (ADR-0035), AC-3: the guest directory's value objects. Pure; no I/O.

Against the T4 skeleton every `__post_init__` is a no-op, so each "refused" test goes red on
`DID NOT RAISE`. `GuestSessionPage`'s "next_cursor iff more" is the query's job (the VO cannot know
whether more exist), so it is asserted in the adapter tests, not here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.errors import InvalidGuestSessionCursor
from tailorcraft.domain.retention.guest_sessions import (
    GuestSessionCursor,
    GuestSessionPage,
    GuestSessionSummary,
)
from tailorcraft.domain.shared.errors import InvariantViolated

_ID = GuestSessionId(value=UUID("77777777-7777-7777-8777-777777777777"))
_CREATED = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
_EXPIRES = _CREATED + timedelta(hours=24)


def _summary(**changes: object) -> GuestSessionSummary:
    base = GuestSessionSummary(
        session_id=_ID,
        created_at=_CREATED,
        expires_at=_EXPIRES,
        base_cvs=0,
        job_postings=0,
        tailoring_runs=0,
        export_jobs=0,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_summary_accepts_zero_counts_and_aware_instants() -> None:
    assert _summary().expires_at == _EXPIRES


@pytest.mark.parametrize("field", ["base_cvs", "job_postings", "tailoring_runs", "export_jobs"])
def test_summary_refuses_a_negative_count(field: str) -> None:
    with pytest.raises(InvariantViolated):
        _summary(**{field: -1})


@pytest.mark.parametrize("field", ["created_at", "expires_at"])
def test_summary_refuses_a_naive_instant(field: str) -> None:
    with pytest.raises(InvariantViolated):
        _summary(**{field: datetime(2026, 10, 2, 10, 0, 0)})


def test_summary_refuses_expiry_equal_to_creation() -> None:
    with pytest.raises(InvariantViolated):
        _summary(expires_at=_CREATED)


def test_summary_refuses_expiry_before_creation() -> None:
    with pytest.raises(InvariantViolated):
        _summary(expires_at=_CREATED - timedelta(seconds=1))


def test_summary_accepts_expiry_one_second_after_creation() -> None:
    assert _summary(expires_at=_CREATED + timedelta(seconds=1)).created_at == _CREATED


def test_summary_accepts_a_non_utc_aware_instant() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert _summary(expires_at=_EXPIRES.astimezone(plus_two)).expires_at == _EXPIRES


def test_cursor_accepts_an_aware_instant() -> None:
    assert GuestSessionCursor(expires_at=_EXPIRES, session_id=_ID).session_id == _ID


def test_cursor_refuses_a_naive_instant() -> None:
    with pytest.raises(InvalidGuestSessionCursor):
        GuestSessionCursor(expires_at=datetime(2026, 10, 2, 10, 0, 0), session_id=_ID)


def test_page_holds_its_items_and_cursor() -> None:
    cursor = GuestSessionCursor(expires_at=_EXPIRES, session_id=_ID)
    page = GuestSessionPage(items=(_summary(),), next_cursor=cursor)
    assert page.items == (_summary(),)
    assert page.next_cursor == cursor
