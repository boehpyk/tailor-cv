"""Domain events for `identity` (slice 2.1): the five events' exact field sets (AC-5).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Checked structurally via
`dataclasses.fields()`, the same form `tests/unit/tailoring/test_events.py` and
`tests/unit/export/test_events.py` use for the identical reason: `LoggingEventPublisher` logs
*every field of every event it receives*, so an event's field set *is* a log field set. A test that
only grepped for the string `"email"` would pass vacuously the day a field nobody predicted
(`account_email`, `contact`, `address`, ...) is added under a different name.

These tests need no instance and no working aggregate: the dataclass field list *is* the signature,
and `events.py` is written whole at the skeleton stage (it has no `NotImplementedError` bodies) — so
every test below is green on arrival, and stays that way unless a field set drifts.
"""

from __future__ import annotations

import dataclasses

from tailorcraft.domain.identity.events import (
    LoggedIn,
    LoggedOut,
    RefreshTokenReuseDetected,
    UserPasswordRehashed,
    UserRegistered,
)

_ALL_EVENTS = (
    UserRegistered,
    UserPasswordRehashed,
    LoggedIn,
    LoggedOut,
    RefreshTokenReuseDetected,
)


def test_user_registered_field_set_is_exactly_the_agreed_fields() -> None:
    field_names = {field.name for field in dataclasses.fields(UserRegistered)}

    assert field_names == {"user_id", "occurred_at"}


def test_user_password_rehashed_field_set_is_exactly_the_agreed_fields() -> None:
    """Neither the old hash nor the new one: a hash is a guessing target, not a fact worth a log
    line, and `password_updated_at` is exactly `occurred_at`."""
    field_names = {field.name for field in dataclasses.fields(UserPasswordRehashed)}

    assert field_names == {"user_id", "occurred_at"}


def test_logged_in_field_set_is_exactly_the_agreed_fields() -> None:
    """No token hash: a `Login`'s current hash is derivable from the row it just wrote, and this
    event exists to say *that* a login began, not to be a second copy of the token."""
    field_names = {field.name for field in dataclasses.fields(LoggedIn)}

    assert field_names == {"user_id", "login_id", "occurred_at"}


def test_logged_out_field_set_is_exactly_the_agreed_fields() -> None:
    field_names = {field.name for field in dataclasses.fields(LoggedOut)}

    assert field_names == {"user_id", "login_id", "occurred_at"}


def test_refresh_token_reuse_detected_field_set_is_exactly_the_agreed_fields() -> None:
    """The two generations and nothing else: "one behind, ten seconds late" is a lost response,
    "twelve behind" is theft — that distinction is the whole value of this event, and neither hash
    is in it (a hash in this event would be half a stolen credential in a log line)."""
    field_names = {field.name for field in dataclasses.fields(RefreshTokenReuseDetected)}

    assert field_names == {
        "user_id",
        "login_id",
        "generation_presented",
        "generation_current",
        "occurred_at",
    }


# --- AC-5, restated as one sweep: no identity event carries an email anywhere ----------------------


def test_no_identity_event_has_a_field_whose_name_mentions_email() -> None:
    """AC-5's own sentence: "never an email". Swept once across all five events rather than
    trusted to the individual field-set assertions above, so a sixth event added later without its
    own test still trips this one — `_ALL_EVENTS` is the list a reviewer extends, not a place a new
    event quietly goes unchecked."""
    for event_cls in _ALL_EVENTS:
        field_names = {field.name for field in dataclasses.fields(event_cls)}
        assert not any("email" in name.lower() for name in field_names), (
            f"{event_cls.__name__} has a field whose name mentions 'email': {field_names}"
        )
