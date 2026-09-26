"""`Owner = GuestOwner | UserOwner` (`domain/identity/ownership.py`) — ADR-0022, AC-1.

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. `ownership.py` shipped complete in
T4 (it is pure data, nothing to defer to GREEN), so this file mostly *pins* behaviour that already
holds rather than driving new implementation — legitimate per sdlc.md §2 ("tests of already-complete
pure data ... will pass"). It exists anyway, and is written from AC-1's wording rather than from the
module, so that a future edit weakening equality, adding a `kind` field, or introducing a shared base
class turns one of these red rather than only being caught by a reviewer's eye.
"""

from __future__ import annotations

import dataclasses
from typing import assert_never
from uuid import UUID

import pytest

from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId

_SESSION_UUID = UUID("11111111-1111-7111-8111-111111111111")
_USER_UUID = UUID("22222222-2222-7222-8222-222222222222")

# Deliberately the *same* UUID value for both variants below — the point of the cross-variant
# inequality test is that identical bytes are not enough to make a GuestOwner and a UserOwner equal.
_SHARED_UUID = UUID("33333333-3333-7333-8333-333333333333")


# --- frozen, slotted --------------------------------------------------------------------------


def test_guest_owner_is_frozen() -> None:
    owner = GuestOwner(GuestSessionId(_SESSION_UUID))

    with pytest.raises(dataclasses.FrozenInstanceError):
        owner.guest_session_id = GuestSessionId(_USER_UUID)  # type: ignore[misc]


def test_user_owner_is_frozen() -> None:
    owner = UserOwner(UserId(_USER_UUID))

    with pytest.raises(dataclasses.FrozenInstanceError):
        owner.user_id = UserId(_SESSION_UUID)  # type: ignore[misc]


def test_guest_owner_is_slotted() -> None:
    """`slots=True` means no `__dict__` — a stray attribute cannot be bolted on later."""
    owner = GuestOwner(GuestSessionId(_SESSION_UUID))

    assert not hasattr(owner, "__dict__")


def test_user_owner_is_slotted() -> None:
    owner = UserOwner(UserId(_USER_UUID))

    assert not hasattr(owner, "__dict__")


# --- value equality ------------------------------------------------------------------------------


def test_guest_owner_equals_another_guest_owner_over_the_same_session_id() -> None:
    a = GuestOwner(GuestSessionId(_SESSION_UUID))
    b = GuestOwner(GuestSessionId(_SESSION_UUID))

    assert a == b


def test_guest_owner_does_not_equal_a_guest_owner_over_a_different_session_id() -> None:
    a = GuestOwner(GuestSessionId(_SESSION_UUID))
    b = GuestOwner(GuestSessionId(_USER_UUID))

    assert a != b


def test_user_owner_equals_another_user_owner_over_the_same_user_id() -> None:
    a = UserOwner(UserId(_USER_UUID))
    b = UserOwner(UserId(_USER_UUID))

    assert a == b


def test_user_owner_does_not_equal_a_user_owner_over_a_different_user_id() -> None:
    a = UserOwner(UserId(_USER_UUID))
    b = UserOwner(UserId(_SESSION_UUID))

    assert a != b


def test_guest_owner_never_equals_user_owner_over_the_same_uuid() -> None:
    """AC-1's headline claim: identical bytes, different variant, never equal. Dataclass equality
    compares the class before it compares a field, so this holds even though `GuestSessionId` and
    `UserId` are themselves distinct value-object types wrapping the same `UUID`."""
    # Typed as `Owner` (the union both variants belong to), not as `GuestOwner`/`UserOwner`
    # directly: comparing the two concrete dataclasses with `!=` is a `mypy --strict`
    # "non-overlapping equality" error, correctly — they share no fields, so a plain `!=` between
    # them would ordinarily be a caller's bug. Widening to `Owner` is what the exhaustive `match` in
    # `_describe` above and in `BaseCv.owner` actually do, so it is also the realistic case: two
    # values a caller obtained as `Owner` and is comparing without knowing which variant either is.
    guest: Owner = GuestOwner(GuestSessionId(_SHARED_UUID))
    user: Owner = UserOwner(UserId(_SHARED_UUID))

    assert guest != user
    assert user != guest


def test_guest_owner_does_not_equal_a_plain_guest_session_id() -> None:
    """Guards against a future `__eq__` that unwraps to compare the inner id directly — `Owner` is a
    fact about a *row*, not an alias for the id it wraps.

    `GuestSessionId` is not part of the `Owner` union, so this comparison has no realistic typed
    caller and is deliberately silenced rather than widened: unlike the test above, there is no
    honest common type to compare through."""
    session_id = GuestSessionId(_SESSION_UUID)

    assert GuestOwner(session_id) != session_id  # type: ignore[comparison-overlap]


# --- no shared base class, no shared Protocol, no `kind` field ---------------------------------


def test_guest_owner_and_user_owner_share_no_base_class_beyond_object() -> None:
    shared = set(GuestOwner.__mro__) & set(UserOwner.__mro__)

    assert shared == {object}


def test_owner_variants_carry_no_kind_field() -> None:
    """The type *is* the kind (AC-1) — a `kind: str` discriminator would be a second, disagreeable
    representation of the same fact the class already carries."""
    guest_field_names = {field.name for field in dataclasses.fields(GuestOwner)}
    user_field_names = {field.name for field in dataclasses.fields(UserOwner)}

    assert guest_field_names == {"guest_session_id"}
    assert user_field_names == {"user_id"}
    assert "kind" not in guest_field_names
    assert "kind" not in user_field_names


def test_neither_owner_variant_defines_a_predicate_method() -> None:
    """ "Not `is_guest()`, not `matches(requester)`" (the module docstring, quoting ADR-0010): the
    only operation either variant offers is `==`. A predicate method would invite exactly the
    conflation ADR-0010 forbids, so its absence is asserted rather than assumed.

    `vars(GuestOwner)` also holds the `slots=True` field descriptor (`guest_session_id`, a
    non-callable `member_descriptor`) — filtering on `callable()` keeps the check about *methods*
    rather than failing on the field the dataclass is required to have.
    """
    guest_methods = {
        name
        for name, value in vars(GuestOwner).items()
        if not name.startswith("__") and callable(value)
    }
    user_methods = {
        name
        for name, value in vars(UserOwner).items()
        if not name.startswith("__") and callable(value)
    }

    assert guest_methods == set()
    assert user_methods == set()


# --- exhaustive `match` over the sum type (AC-2's mechanism, pinned here on the type itself) ----


def _describe(owner: Owner) -> str:
    """A minimal consumer in the shape AC-2 describes: `match` over every `Owner` variant with
    `assert_never` in the default arm. `mypy --strict` is what proves a third variant would be a
    compile-time error at every such `match` — this test proves only that the two existing variants
    dispatch to the correct arm at runtime."""
    match owner:
        case GuestOwner():
            return "guest"
        case UserOwner():
            return "user"
        case _:
            assert_never(owner)


def test_match_dispatches_guest_owner_to_the_guest_arm() -> None:
    assert _describe(GuestOwner(GuestSessionId(_SESSION_UUID))) == "guest"


def test_match_dispatches_user_owner_to_the_user_arm() -> None:
    assert _describe(UserOwner(UserId(_USER_UUID))) == "user"
