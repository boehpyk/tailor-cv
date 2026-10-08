"""Who may reach what on the tracking routes (slice 3.1, T21 RED): AC-22, AC-23, AC-29 and T-8, T-9,
T-13, T-23.

- **AC-22.** Every one of the five routes depends on `require_user` only. The refusals (no bearer,
  forged bearer, a guest cookie alone) already pass against the skeleton — `require_user` is 2.1's
  real code — and each is paired with the discriminating positive that cannot: a **valid** bearer
  whose account is gone must be 401 `not_signed_in`, which only a handler that resolves the user
  can answer; and a valid bearer **plus** a valid `__Host-tc_guest` cookie sees only the user's
  board and
  cannot track the guest's run.
- **AC-23.** Alice and Bob each own a succeeded run and a card; guest G owns a succeeded run.
  Alice's bearer on Bob's card id (`PUT …/stage`, `PUT …/title`, `DELETE`) is **404 with a body
  byte-identical to a nonexistent id's**; Alice tracking Bob's run, G's run or a nonexistent run is
  **404 `tailoring_run_not_found`**, byte-identical. Bob's card and G's run are untouched.
- **AC-29 (spec ambiguity resolved).** The spec says "after every flow in AC-38's harness", but
  AC-38 is the React *Add to board* behaviour; the end-to-end flow is **AC-42's**. The invariant is
  asserted here after the cross-owner matrix and in `test_tracking_privacy_markers.py` after every
  step of AC-42's flow — both with a positive control (cards actually looked at).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.infrastructure.api.deps import (
    require_guest_session,
    require_user,
    resolve_or_start_guest_session,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    Account,
    error_code,
    mint_guest,
    new_client,
    register,
    seed_entry,
)
from tests.api.test_auth_route_dependency_boundary import (
    _all_dependency_calls,
    _iter_api_routes,
    _route_touches_guest_cookie_by_direct_call,
)
from tests.api.tracking_support import (
    ME_BOARD,
    ME_TRACKED,
    Seeded,
    assert_no_card_crosses_owners,
    card_count,
    card_row,
    seed_card,
)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every write passes a limiter that lives in Redis."""


@dataclass(frozen=True)
class _Route:
    method: str
    template: str
    body: dict[str, object] | None = None

    def url(self, card_id: str | None = None) -> str:
        return self.template.format(card=card_id or str(uuid4()))

    @property
    def label(self) -> str:
        return f"{self.method} {self.template}"


_ROUTES = [
    _Route("GET", ME_BOARD),
    _Route("POST", ME_TRACKED, {"tailoring_run_id": str(uuid4())}),
    _Route("PUT", ME_TRACKED + "/{card}/stage", {"stage": "applied", "version": 1}),
    _Route("PUT", ME_TRACKED + "/{card}/title", {"title": "Acme", "version": 1}),
    _Route("DELETE", ME_TRACKED + "/{card}"),
]
_CARD_ROUTES = [route for route in _ROUTES if "{card}" in route.template]


async def _call(
    client: AsyncClient, route: _Route, url: str, headers: dict[str, str] | None = None
) -> Response:
    return await client.request(route.method, url, json=route.body, headers=headers or {})


def _earlier(clock: FixedClock, minutes: int = 10) -> datetime:
    return clock.now() - timedelta(minutes=minutes)


# --- AC-22 / T-8 / T-9 ----------------------------------------------------------------------------


@pytest.mark.parametrize("route", _ROUTES, ids=lambda r: r.label)
async def test_t8_no_bearer_is_401_invalid_access_token_with_www_authenticate(
    client: AsyncClient, route: _Route
) -> None:
    response = await _call(client, route, route.url())

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"
    assert "www-authenticate" in {name.lower() for name in response.headers}


@pytest.mark.parametrize("route", _ROUTES, ids=lambda r: r.label)
async def test_t8_a_forged_bearer_is_401_invalid_access_token(
    client: AsyncClient, route: _Route
) -> None:
    response = await _call(client, route, route.url(), {"Authorization": "Bearer forged.token.x"})

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"


@pytest.mark.parametrize("route", _ROUTES, ids=lambda r: r.label)
async def test_ac22_a_guest_cookie_alone_authorizes_nothing(
    client: AsyncClient, session: AsyncSession, route: _Route
) -> None:
    await mint_guest(client, session)  # __Host-tc_guest now rides on every request

    response = await _call(client, route, route.url())

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"


@pytest.mark.parametrize("route", _ROUTES, ids=lambda r: r.label)
async def test_t9_a_valid_bearer_whose_account_is_gone_is_401_not_signed_in_and_writes_nothing(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    route: _Route,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The discriminating positive for the refusals above: the token verifies, so only a handler
    that resolves the user can refuse — and it logs `identity.user_missing` with the user id."""
    account = await register(client, settings)
    await session.execute(
        text("DELETE FROM identity_user WHERE id = :id"), {"id": account.user_id.value}
    )
    await session.commit()  # erased for real: a refused request's rollback must not bring it back

    with caplog.at_level(logging.INFO):
        response = await _call(client, route, route.url(), account.headers)

    assert response.status_code == 401, response.text
    assert error_code(response) == "not_signed_in"
    assert await card_count(session, account.user_id.value) == 0
    assert "identity.user_missing" in caplog.text
    assert str(account.user_id.value) in caplog.text


async def test_ac22_a_valid_bearer_beside_a_valid_guest_cookie_sees_the_users_board_only(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    mine = await seed_card(session, settings, account, at=_earlier(clock))
    guest = await mint_guest(client, session)
    await seed_entry(session, settings, guest, at=_earlier(clock), ready_formats=())

    response = await client.get(ME_BOARD, headers=account.headers)

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(mine.card_id)]


async def test_ac22_the_guest_cookie_does_not_let_a_user_track_the_guests_run(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    """The cookie rides on the request (this client minted it) and still authorizes nothing: the
    guest's run is a 404 for the bearer, and nothing is written."""
    account = await register(client, settings)
    guest = await mint_guest(client, session)
    guest_entry = await seed_entry(session, settings, guest, at=_earlier(clock), ready_formats=())
    own = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())

    refused = await client.post(
        ME_TRACKED,
        json={"tailoring_run_id": str(guest_entry.run_id.value)},
        headers=account.headers,
    )
    accepted = await client.post(
        ME_TRACKED, json={"tailoring_run_id": str(own.run_id.value)}, headers=account.headers
    )

    assert refused.status_code == 404, refused.text
    assert error_code(refused) == "tailoring_run_not_found"
    assert accepted.status_code == 201, accepted.text
    assert await card_count(session, account.user_id.value) == 1


def test_ac22_the_walker_finds_every_route_on_require_user_alone(app: FastAPI) -> None:
    expected = {
        f"{route.method} {route.template}".replace("{card}", "{tracked_application_id}")
        for route in _ROUTES
    }
    found: dict[str, APIRoute] = {}
    for api_route in _iter_api_routes(app.routes):
        for method in api_route.methods or ():
            found[f"{method} {api_route.path}"] = api_route

    assert expected <= set(found), sorted(expected - set(found))
    for label in sorted(expected):
        calls = _all_dependency_calls(found[label].dependant)
        assert require_user in calls, label
        assert require_guest_session not in calls, label
        assert resolve_or_start_guest_session not in calls, label
        assert not _route_touches_guest_cookie_by_direct_call(found[label]), label


def test_ac22_the_transfer_route_set_is_still_exactly_the_claim_route(app: FastAPI) -> None:
    """2.4's AST scan, restated beside the new routes: routes that depend on `require_user` **and**
    touch `__Host-tc_guest` — by the dependency graph or by a direct call in the body — are
    exactly `{POST /api/me/guest-work/claim}`. Passes today; it is the guard against a tracking route
    drifting into the set."""
    violations: set[str] = set()
    for route in _iter_api_routes(app.routes):
        calls = _all_dependency_calls(route.dependant)
        touches = (
            require_guest_session in calls
            or resolve_or_start_guest_session in calls
            or _route_touches_guest_cookie_by_direct_call(route)
        )
        if require_user in calls and touches:
            violations.update(f"{method} {route.path}" for method in sorted(route.methods or ()))

    assert violations == {"POST /api/me/guest-work/claim"}, sorted(violations)


# --- AC-23: the A/B/G matrix -----------------------------------------------------------------------


@dataclass
class _World:
    alice: Account
    bob: Account
    alice_card: Seeded
    bob_card: Seeded
    guest_run_id: str


async def _world(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> _World:
    alice = await register(client, settings)
    bob = await register(client, settings)
    guest = await mint_guest(client, session)
    at = _earlier(clock)
    alice_card = await seed_card(session, settings, alice, at=at, title="Alice's")
    bob_card = await seed_card(session, settings, bob, at=at, title="Bob's")
    guest_entry = await seed_entry(session, settings, guest, at=at, ready_formats=())
    return _World(alice, bob, alice_card, bob_card, str(guest_entry.run_id.value))


@pytest.mark.parametrize("route", _CARD_ROUTES, ids=lambda r: r.label)
async def test_ac23_another_users_card_is_404_byte_identical_to_a_nonexistent_one_and_untouched(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    route: _Route,
) -> None:
    world = await _world(client, settings, session, clock)
    before = await card_row(session, world.bob_card.card_id)
    assert before is not None

    theirs = await _call(client, route, route.url(str(world.bob_card.card_id)), world.alice.headers)
    nobodys = await _call(client, route, route.url(str(uuid4())), world.alice.headers)

    assert theirs.status_code == 404, theirs.text
    assert nobodys.status_code == 404, nobodys.text
    assert error_code(theirs) == "tracked_application_not_found"
    assert theirs.content == nobodys.content
    assert await card_row(session, world.bob_card.card_id) == before
    assert await card_row(session, world.alice_card.card_id) is not None


@pytest.mark.parametrize("whose", ["bob", "guest"])
async def test_ac23_tracking_another_owners_run_is_404_byte_identical_to_a_nonexistent_run(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    whose: str,
) -> None:
    world = await _world(client, settings, session, clock)
    target = str(world.bob_card.run_id) if whose == "bob" else world.guest_run_id

    theirs = await client.post(
        ME_TRACKED, json={"tailoring_run_id": target}, headers=world.alice.headers
    )
    nobodys = await client.post(
        ME_TRACKED, json={"tailoring_run_id": str(uuid4())}, headers=world.alice.headers
    )

    assert theirs.status_code == 404, theirs.text
    assert nobodys.status_code == 404, nobodys.text
    assert error_code(theirs) == "tailoring_run_not_found"
    assert theirs.content == nobodys.content
    assert await card_count(session, world.alice.user_id.value) == 1, "only her own card exists"
    assert await card_count(session, world.bob.user_id.value) == 1


async def test_ac23_a_run_another_user_tracks_is_404_never_already_tracked(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    """The uniqueness of a run is per run, not per user: a run Bob once tracked is *still* not
    Alice's to track — and must not answer 409 `application_already_tracked` (an oracle for "Bob
    tracks this")."""
    world = await _world(client, settings, session, clock)

    response = await client.post(
        ME_TRACKED,
        json={"tailoring_run_id": str(world.bob_card.run_id)},
        headers=world.alice.headers,
    )

    assert response.status_code == 404, response.text
    assert error_code(response) == "tailoring_run_not_found"
    assert "tracked_application_id" not in response.text


async def test_ac23_the_board_never_lists_another_users_card(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    world = await _world(client, settings, session, clock)

    alices = await client.get(ME_BOARD, headers=world.alice.headers)
    bobs = await client.get(ME_BOARD, headers=world.bob.headers)

    assert alices.status_code == 200, alices.text
    assert bobs.status_code == 200, bobs.text
    assert [i["id"] for i in alices.json()["items"]] == [str(world.alice_card.card_id)]
    assert [i["id"] for i in bobs.json()["items"]] == [str(world.bob_card.card_id)]
    assert "Bob's" not in alices.text
    assert "Alice's" not in bobs.text


# --- AC-29: no ownership graph crosses owners ------------------------------------------------------


async def test_ac29_after_every_cross_owner_attempt_no_card_crosses_owners(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    """Alice and Bob each track their own run through the route; every cross-owner attempt (each
    way, plus the guest's run) is refused; then the invariant — and a positive control: both cards
    exist, so "zero crossing" is not "zero cards"."""
    alice = await register(client, settings)
    bob = await register(client, settings)
    guest = await mint_guest(client, session)
    alice_entry = await seed_entry(
        session, settings, alice.owner, at=_earlier(clock), ready_formats=()
    )
    bob_entry = await seed_entry(session, settings, bob.owner, at=_earlier(clock), ready_formats=())
    guest_entry = await seed_entry(session, settings, guest, at=_earlier(clock), ready_formats=())

    own_a = await client.post(
        ME_TRACKED, json={"tailoring_run_id": str(alice_entry.run_id.value)}, headers=alice.headers
    )
    own_b = await client.post(
        ME_TRACKED, json={"tailoring_run_id": str(bob_entry.run_id.value)}, headers=bob.headers
    )
    assert own_a.status_code == 201, own_a.text
    assert own_b.status_code == 201, own_b.text
    refusals = [
        await client.post(
            ME_TRACKED,
            json={"tailoring_run_id": str(bob_entry.run_id.value)},
            headers=alice.headers,
        ),
        await client.post(
            ME_TRACKED,
            json={"tailoring_run_id": str(alice_entry.run_id.value)},
            headers=bob.headers,
        ),
        await client.post(
            ME_TRACKED,
            json={"tailoring_run_id": str(guest_entry.run_id.value)},
            headers=alice.headers,
        ),
    ]

    assert [r.status_code for r in refusals] == [404, 404, 404]
    checked = await assert_no_card_crosses_owners(session, [alice.user_id.value, bob.user_id.value])
    assert checked == 2, "the positive control: both users' cards were looked at"
