"""The `Login` aggregate: AC-6's generation arithmetic and race-grace boundaries, AC-5's recorded
events, and AC-1's mapped-class-constructor hole, restated for `Login` (task-list.md T8:
`Login(_generation=5)` must raise `TypeError`).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Every assertion here comes from
`domain/identity/login.py`'s own docstrings and AC-6's text — "was this token stolen?" is a
comparison of two integers and two instants, and that is exactly what is exercised below with no
I/O at all.

There is no other way into a real `Login` than `start`, followed by any number of legal `rotate`
calls — so every fixture below is built that way rather than by touching a private attribute.

**RED-first trap** (task-list.md, top of file): `NotImplementedError` subclasses `RuntimeError`, so
every `pytest.raises(...)` below names the exact domain exception. `TokenHash` is itself still a
skeleton at this point in the build order (T6 GREEN has not landed), so most tests below actually
fail one layer down, inside `TokenHash.__post_init__` — still `NotImplementedError`, still not an
`ImportError`. The two constructor-hole tests at the bottom use placeholder strings instead of real
value objects for exactly this reason, and stay green on arrival regardless.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.errors import LoginExpired
from tailorcraft.domain.identity.events import LoggedIn, LoggedOut, RefreshTokenReuseDetected
from tailorcraft.domain.identity.login import REFRESH_RACE_GRACE, Login
from tailorcraft.domain.identity.value_objects import (
    LoginId,
    RetiredTokenVerdict,
    TokenHash,
    UserId,
)
from tailorcraft.domain.shared.errors import InvariantViolated

_LOGIN_ID = LoginId(value=UUID("55555555-5555-7555-8555-555555555555"))
_USER_ID = UserId(value=UUID("44444444-4444-7444-8444-444444444444"))
_STARTED_AT = datetime(2026, 9, 23, 10, 0, 0, tzinfo=UTC)  # whole-second, ADR-0007
_LIFETIME = timedelta(days=30)


def _token_hash(value: str = "a" * 64) -> TokenHash:
    return TokenHash(value)


def _started(*, at: datetime = _STARTED_AT, lifetime: timedelta = _LIFETIME) -> Login:
    """The only constructor, so every other builder below starts here (technical plan §1)."""
    return Login.start(_LOGIN_ID, _USER_ID, _token_hash(), at, lifetime)


# --- AC-6 / AC-5: start ------------------------------------------------------------------------


def test_start_sets_generation_one_rotated_at_none_version_one_and_expires_at() -> None:
    login = _started()

    assert login.generation == 1
    assert login.rotated_at is None
    assert login.expires_at == _STARTED_AT + _LIFETIME
    assert login.version == 1
    assert login.created_at == _STARTED_AT
    assert login.current_token_hash == _token_hash()


def test_start_records_exactly_one_logged_in() -> None:
    login = _started()

    events = login.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, LoggedIn)
    assert event.user_id == _USER_ID
    assert event.login_id == _LOGIN_ID
    assert event.occurred_at == _STARTED_AT


def test_start_refuses_a_zero_lifetime() -> None:
    with pytest.raises(InvariantViolated):
        Login.start(_LOGIN_ID, _USER_ID, _token_hash(), _STARTED_AT, timedelta(seconds=0))


def test_start_refuses_a_negative_lifetime() -> None:
    with pytest.raises(InvariantViolated):
        Login.start(_LOGIN_ID, _USER_ID, _token_hash(), _STARTED_AT, timedelta(seconds=-1))


# --- AC-6: rotate advances generation by exactly one, leaves expires_at and version unchanged -----


def test_rotate_advances_generation_from_one_to_two_and_returns_the_retired_token() -> None:
    login = _started()
    new_hash = _token_hash("b" * 64)
    rotated_at = _STARTED_AT + timedelta(minutes=15)

    retired = login.rotate(new_hash, rotated_at)

    assert login.generation == 2
    assert login.rotated_at == rotated_at
    assert login.current_token_hash == new_hash
    assert retired.token_hash == _token_hash()  # the old (generation-1) hash
    assert retired.generation == 1
    assert retired.retired_at == rotated_at


def test_rotate_does_not_change_expires_at() -> None:
    """Absolute lifetime (OQ-9): a rotation on day 29 does not reset the clock to 30 days."""
    login = _started()
    expires_before = login.expires_at

    login.rotate(_token_hash("b" * 64), _STARTED_AT + timedelta(minutes=15))

    assert login.expires_at == expires_before


def test_rotate_does_not_change_version() -> None:
    """`version` is the repository's, not the aggregate's (`login.py`'s own docstring) —
    `save_rotation` bumps it; `rotate` itself must not."""
    login = _started()

    login.rotate(_token_hash("b" * 64), _STARTED_AT + timedelta(minutes=15))

    assert login.version == 1


def test_three_successive_rotations_advance_generation_one_two_three() -> None:
    login = _started()

    login.rotate(_token_hash("b" * 64), _STARTED_AT + timedelta(minutes=15))
    assert login.generation == 2

    login.rotate(_token_hash("c" * 64), _STARTED_AT + timedelta(minutes=30))
    assert login.generation == 3


def test_rotate_records_no_event() -> None:
    """One every 15 minutes per tab would be a log flood with no listener (`login.py`'s own
    docstring)."""
    login = _started()
    login.release_events()  # discard LoggedIn

    login.rotate(_token_hash("b" * 64), _STARTED_AT + timedelta(minutes=15))

    assert login.release_events() == ()


def test_rotate_at_exactly_expires_at_raises_login_expired() -> None:
    """Inclusive, matching `GuestSession.is_expired` (AC-6): a rotation checked in the same whole
    second the login dies must be refused, not allowed through on a technicality."""
    login = _started()

    with pytest.raises(LoginExpired):
        login.rotate(_token_hash("b" * 64), login.expires_at)


def test_rotate_one_second_before_expires_at_succeeds() -> None:
    login = _started()
    one_second_before = login.expires_at - timedelta(seconds=1)

    login.rotate(_token_hash("b" * 64), one_second_before)  # must not raise

    assert login.generation == 2


# --- AC-6: judge_retired — the 10-second boundary pinned on both sides ----------------------------


def test_judge_retired_at_rotated_at_plus_the_grace_is_raced() -> None:
    """The grace is inclusive on its own boundary: `at - rotated_at == REFRESH_RACE_GRACE` is still
    `RACED` (`login.py`'s own docstring)."""
    login = _started()
    rotated_at = _STARTED_AT + timedelta(minutes=15)
    login.rotate(_token_hash("b" * 64), rotated_at)  # now generation 2

    verdict = login.judge_retired(1, rotated_at + REFRESH_RACE_GRACE)

    assert verdict is RetiredTokenVerdict.RACED


def test_judge_retired_one_second_past_the_grace_is_reused() -> None:
    login = _started()
    rotated_at = _STARTED_AT + timedelta(minutes=15)
    login.rotate(_token_hash("b" * 64), rotated_at)

    verdict = login.judge_retired(1, rotated_at + REFRESH_RACE_GRACE + timedelta(seconds=1))

    assert verdict is RetiredTokenVerdict.REUSED


def test_judge_retired_two_generations_behind_is_reused_even_at_zero_seconds() -> None:
    """A generation two behind is `REUSED` even at 0 s (AC-6) — the RACED clause only ever answers
    for the *immediate* predecessor."""
    login = _started()
    first_rotation = _STARTED_AT + timedelta(minutes=15)
    login.rotate(_token_hash("b" * 64), first_rotation)  # generation 2
    second_rotation = first_rotation + timedelta(minutes=15)
    login.rotate(_token_hash("c" * 64), second_rotation)  # generation 3

    verdict = login.judge_retired(1, second_rotation)  # 0 s after the second rotation

    assert verdict is RetiredTokenVerdict.REUSED


def test_judge_retired_on_a_never_rotated_login_is_reused_regardless_of_generation() -> None:
    """`rotated_at` is `None` before the first rotation, so the RACED clause — which compares
    `at` against `rotated_at` — has nothing to compare against: every presentation on a login that
    has never rotated is `REUSED`, whatever generation it names (task-list.md T8)."""
    login = _started()

    verdict = login.judge_retired(0, _STARTED_AT)

    assert verdict is RetiredTokenVerdict.REUSED


def test_judge_retired_reused_records_refresh_token_reuse_detected_with_both_generations() -> None:
    login = _started()
    rotated_at = _STARTED_AT + timedelta(minutes=15)
    login.rotate(_token_hash("b" * 64), rotated_at)  # generation 2
    login.release_events()  # discard LoggedIn (rotate itself records nothing)
    presented_at = rotated_at + REFRESH_RACE_GRACE + timedelta(seconds=1)

    login.judge_retired(1, presented_at)

    events = login.release_events()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, RefreshTokenReuseDetected)
    assert event.user_id == _USER_ID
    assert event.login_id == _LOGIN_ID
    assert event.generation_presented == 1
    assert event.generation_current == 2
    assert event.occurred_at == presented_at


def test_judge_retired_raced_records_no_event() -> None:
    login = _started()
    rotated_at = _STARTED_AT + timedelta(minutes=15)
    login.rotate(_token_hash("b" * 64), rotated_at)
    login.release_events()

    login.judge_retired(1, rotated_at + REFRESH_RACE_GRACE)

    assert login.release_events() == ()


def test_judge_retired_changes_no_state_either_way() -> None:
    """I-23: RACED must leave everything as it was; REUSED's revocation is the repository's
    deletion, never a flag on the aggregate — so neither outcome may move `generation`,
    `current_token_hash`, `rotated_at` or `version`."""
    login = _started()
    rotated_at = _STARTED_AT + timedelta(minutes=15)
    login.rotate(_token_hash("b" * 64), rotated_at)
    generation_before = login.generation
    hash_before = login.current_token_hash
    rotated_at_before = login.rotated_at
    version_before = login.version

    login.judge_retired(1, rotated_at + REFRESH_RACE_GRACE)  # RACED
    login.judge_retired(1, rotated_at + REFRESH_RACE_GRACE + timedelta(seconds=1))  # REUSED

    assert login.generation == generation_before
    assert login.current_token_hash == hash_before
    assert login.rotated_at == rotated_at_before
    assert login.version == version_before


def test_judge_retired_on_an_expired_login_raises_login_expired() -> None:
    login = _started()

    with pytest.raises(LoginExpired):
        login.judge_retired(1, login.expires_at)


# --- record_logout -------------------------------------------------------------------------------


def test_record_logout_records_exactly_one_logged_out() -> None:
    login = _started()
    login.release_events()  # discard LoggedIn
    logout_at = _STARTED_AT + timedelta(hours=1)

    login.record_logout(logout_at)

    events = login.release_events()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, LoggedOut)
    assert event.user_id == _USER_ID
    assert event.login_id == _LOGIN_ID
    assert event.occurred_at == logout_at


# --- AC-1 / CLAUDE.md: the mapped-class default-constructor hole, restated for Login ---------------
#
# Both green on arrival: `__init__` takes no arguments today, so Python's own signature check
# refuses both calls before any body runs. Placeholder strings stand in for real value objects
# deliberately — these tests are about the constructor's signature, not `Login`'s business rules.


def test_login_cannot_be_constructed_with_the_start_arguments() -> None:
    """`start` is the only constructor. A direct call — even with every argument it itself needs —
    must be refused, because a second way in is a second place the generation invariant could be
    bypassed."""
    with pytest.raises(TypeError):
        Login(  # type: ignore[call-arg]
            id=_LOGIN_ID,
            user_id=_USER_ID,
            token_hash="placeholder",
            at=_STARTED_AT,
            lifetime=_LIFETIME,
        )


def test_login_cannot_be_constructed_via_the_mapped_attribute_names() -> None:
    """The hole task-list.md T8 names directly: `Login(_generation=5)` would be a family that
    skipped four rotations it never had — no owner user, no token, no `LoggedIn` event, and a
    generation that claims a history nothing produced."""
    with pytest.raises(TypeError):
        Login(_generation=5)  # type: ignore[call-arg]
