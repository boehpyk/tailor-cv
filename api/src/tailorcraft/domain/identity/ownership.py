"""Who a row belongs to: a guest session or a user — never both, never neither (ADR-0022).

`Owner` is a **sum type**: `GuestOwner | UserOwner`, two frozen dataclasses and a union alias. A row
with two owners or none cannot be expressed in this type, which is the whole point — a row nobody
owns is a row nobody can authorize. Code that has to branch on the variant uses `match` with
`assert_never` in the default arm, and `mypy --strict` is then the exhaustiveness checker: a third
variant would be a type error at every `match` that forgot it.

**"Owner" is a word about rows, never about requesters.** The requester is whoever presented a
credential; the owner is what a row records. Authorization is value equality between the two —
`cv.owner == UserOwner(requester_id)` — and nothing more.

That is also why neither variant has a method. Not `is_guest()`, not `matches(requester)`: the one
operation anyone needs is `==`, which the dataclasses already give, and a predicate would invite the
`if owner.is_guest() or …` conflation ADR-0010 forbids ("a guest session is not a weak login").

**No base class and no shared Protocol**, and no `kind: str` field either — the *type* is the kind.
A `GuestOwner` never equals a `UserOwner`, even over the same UUID, because dataclass equality
compares the class before it compares a field.

It lives in `identity` because every context already imports `GuestSessionId` from here, and
`domain/shared` must not import a context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId


@dataclass(frozen=True, slots=True)
class GuestOwner:
    """The row belongs to an anonymous browser session and lives at most as long as it (ADR-0006)."""

    guest_session_id: GuestSessionId


@dataclass(frozen=True, slots=True)
class UserOwner:
    """The row belongs to a registered user and lives until they delete it (ADR-0006 amendment)."""

    user_id: UserId


# `TypeAlias` rather than the 3.12 `type Owner = …` statement on purpose: a `type` statement builds
# a `TypeAliasType`, which `isinstance(x, Owner)` and `match x: case Owner()` both refuse at runtime,
# while this plain union works in both places and still reads as a named type to mypy.
Owner: TypeAlias = GuestOwner | UserOwner  # noqa: UP040 — see the comment above
