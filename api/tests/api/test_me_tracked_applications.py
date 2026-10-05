"""The tracking HTTP contract (slice 3.1, T21 RED): AC-21, AC-25, AC-26, AC-30 and the failure-contract
rows T-12…T-16, T-20, T-22, T-24…T-28, T-30, T-31, T-36, T-37 that are reachable over HTTP.

Red-first against the T20 skeleton, whose handlers raise `NotImplementedError` (a real 500 through
`new_client`), so a red reads `assert 500 == 201` — on the assertion about the route.

**Honest note on what already passes against the skeleton:** the 401s from `require_user`, the 422s
Pydantic raises before a handler runs (unknown stage, unknown key, malformed UUID, `version` below 1,
a missing `title` key on the retitle), and 405 for a method no route answers. Each is paired in this
file with the discriminating positive that cannot pass until the handler is real (a 201 / 200 / 204).

**Hard from the start.** Every step of every flow is a status assertion; nothing is gated on an
earlier step having worked.

Spec ambiguities resolved here, on the plan's §4 rather than on any observed behaviour:

- `Cache-Control: no-store` is asserted on the **success** responses (201, 200, 204) — the ones the
  handler builds. Error bodies are built by the exception handlers; 2.4's AC-24 wording (the
  responses the *handler* builds) is followed, not widened.
- A retitle does not move a card: `stage_changed_at` is unchanged (the aggregate's docstring).
- The message text is pinned only where the spec quotes it (T-12).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.tracking.value_objects import ApplicationStage
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    error_body,
    error_code,
    new_client,
    override_settings,
    register,
    seed_entry,
)
from tests.api.tracking_support import (
    BOARD_BASE_CV_KEYS,
    BOARD_CARD_KEYS,
    BOARD_POSTING_KEYS,
    BOARD_RUN_KEYS,
    ME_BOARD,
    ME_TRACKED,
    STAGES,
    TRACKED_APPLICATION_KEYS,
    assert_no_store,
    card_count,
    card_row,
    seed_card,
    seed_orphan_card,
)
from tests.integration.owners import failed_run, queued_run, running_run


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every write passes a limiter that lives in Redis; the rollback never reaches it."""


def _earlier(clock: FixedClock, minutes: int = 10) -> datetime:
    return clock.now() - timedelta(minutes=minutes)


# ===================================================================================================
# POST /api/me/tracked-applications  (AC-21, AC-25)
# ===================================================================================================


async def test_ac25_tracking_a_succeeded_run_is_201_with_location_and_the_default_stage(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())

    response = await client.post(
        ME_TRACKED, json={"tailoring_run_id": str(entry.run_id.value)}, headers=account.headers
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == TRACKED_APPLICATION_KEYS
    assert body["tailoring_run_id"] == str(entry.run_id.value)
    assert body["stage"] == "to_apply"
    assert body["title"] is None
    assert body["version"] == 1
    assert body["tracked_at"] == body["stage_changed_at"]
    assert response.headers["location"] == f"{ME_TRACKED}/{body['id']}"
    assert_no_store(response)
    assert await card_count(session, account.user_id.value) == 1


async def test_ac25_the_card_is_created_in_the_requested_stage_with_the_trimmed_title(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())

    response = await client.post(
        ME_TRACKED,
        json={
            "tailoring_run_id": str(entry.run_id.value),
            "stage": "interviewing",
            "title": "  Acme — Senior Engineer  ",
        },
        headers=account.headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["stage"] == "interviewing"
    assert response.json()["title"] == "Acme — Senior Engineer"
    stored = await card_row(session, UUID(response.json()["id"]))
    assert stored is not None
    assert str(stored["stage"]) == "interviewing"


async def test_ac25_a_null_title_is_a_card_without_one(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())

    response = await client.post(
        ME_TRACKED,
        json={"tailoring_run_id": str(entry.run_id.value), "title": None},
        headers=account.headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["title"] is None


@pytest.mark.parametrize("stage", STAGES)
async def test_ac25_every_one_of_the_six_stages_is_accepted_on_create(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    stage: str,
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())

    response = await client.post(
        ME_TRACKED,
        json={"tailoring_run_id": str(entry.run_id.value), "stage": stage},
        headers=account.headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["stage"] == stage


@pytest.mark.parametrize("kind", ["queued", "running", "failed"])
async def test_t12_a_run_that_has_not_succeeded_is_409_not_trackable_and_writes_no_row(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    kind: str,
) -> None:
    account = await register(client, settings)
    builders = {"queued": queued_run, "running": running_run, "failed": failed_run}
    at = _earlier(clock)
    # `seed_entry` takes the run's posting from the run when one is given (2.3's rule).
    entry = await seed_entry(
        session,
        settings,
        account.owner,
        at=at,
        run=builders[kind](account.owner, at),
        ready_formats=(),
    )

    response = await client.post(
        ME_TRACKED, json={"tailoring_run_id": str(entry.run_id.value)}, headers=account.headers
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "tailoring_run_not_trackable"
    assert error_body(response)["message"] == (
        "Only a finished tailored application can go on your board."
    )
    assert await card_count(session, account.user_id.value) == 0


async def test_t14_tracking_a_run_twice_is_409_naming_the_existing_card_and_adds_no_row(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())
    body = {"tailoring_run_id": str(entry.run_id.value)}
    first = await client.post(ME_TRACKED, json=body, headers=account.headers)
    assert first.status_code == 201, first.text

    second = await client.post(ME_TRACKED, json=body, headers=account.headers)

    assert second.status_code == 409, second.text
    assert error_code(second) == "application_already_tracked"
    assert error_body(second)["tracked_application_id"] == first.json()["id"]
    assert await card_count(session, account.user_id.value) == 1


async def test_t15_the_per_user_cap_is_409_too_many_tracked_applications_and_adds_no_row(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    override_settings(app, settings, max_tracked_applications_per_user=1)
    account = await register(client, settings)
    first_entry = await seed_entry(
        session, settings, account.owner, at=_earlier(clock), ready_formats=()
    )
    second_entry = await seed_entry(
        session, settings, account.owner, at=_earlier(clock), ready_formats=()
    )
    first = await client.post(
        ME_TRACKED,
        json={"tailoring_run_id": str(first_entry.run_id.value)},
        headers=account.headers,
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        ME_TRACKED,
        json={"tailoring_run_id": str(second_entry.run_id.value)},
        headers=account.headers,
    )

    assert second.status_code == 409, second.text
    assert error_code(second) == "too_many_tracked_applications"
    assert "remove some" in str(error_body(second)["message"])
    assert await card_count(session, account.user_id.value) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"stage": "someday"},
        {"stage": "TO_APPLY"},
        {"stage": None},
        {"unexpected": 1},
        {"title": "x" * 121},
        {"title": "   "},
        {"title": ""},
        {"title": "line one\nline two"},
        {"title": "tab\there"},
        {"title": "nul\x00byte"},
        {"title": 7},
    ],
    ids=[
        "unknown_stage",
        "wrong_case_stage",
        "null_stage",
        "unknown_key",
        "title_121_chars",
        "title_blank",
        "title_empty",
        "title_newline",
        "title_tab",
        "title_nul",
        "title_not_a_string",
    ],
)
async def test_t16_an_invalid_create_body_is_422_validation_error_and_writes_no_row(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    body: dict[str, object],
) -> None:
    """Pairs with `test_ac25_tracking_a_succeeded_run_is_201…`: the run here is a real, trackable
    one, so a 422 is the body's fault and nothing else's."""
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())

    response = await client.post(
        ME_TRACKED,
        json={"tailoring_run_id": str(entry.run_id.value), **body},
        headers=account.headers,
    )

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    assert await card_count(session, account.user_id.value) == 0


async def test_t16_a_title_that_is_refused_is_never_echoed(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())
    marker = f"QA31TITLE-{uuid4().hex}"

    over_long = await client.post(
        ME_TRACKED,
        json={"tailoring_run_id": str(entry.run_id.value), "title": marker + "x" * 120},
        headers=account.headers,
    )
    control = await client.post(
        ME_TRACKED,
        json={"tailoring_run_id": str(entry.run_id.value), "title": marker + "\nx"},
        headers=account.headers,
    )

    for refused in (over_long, control):
        assert refused.status_code == 422, refused.text
        assert marker not in refused.text
    assert await card_count(session, account.user_id.value) == 0


async def test_t16_a_malformed_run_id_is_422(client: AsyncClient, settings: Settings) -> None:
    account = await register(client, settings)

    response = await client.post(
        ME_TRACKED, json={"tailoring_run_id": "not-a-uuid"}, headers=account.headers
    )

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


async def test_t16_a_missing_run_id_is_422(client: AsyncClient, settings: Settings) -> None:
    account = await register(client, settings)

    response = await client.post(ME_TRACKED, json={}, headers=account.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


# ===================================================================================================
# PUT …/stage and PUT …/title  (AC-26)
# ===================================================================================================


async def test_ac26_a_move_is_200_with_the_card_a_new_stage_and_the_next_version(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock), title="Acme — SRE")

    response = await client.put(
        card.stage_url, json={"stage": "applied", "version": 1}, headers=account.headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == TRACKED_APPLICATION_KEYS
    assert body["id"] == str(card.card_id)
    assert body["tailoring_run_id"] == str(card.run_id)
    assert body["stage"] == "applied"
    assert body["title"] == "Acme — SRE"
    assert body["version"] == 2
    assert datetime.fromisoformat(body["stage_changed_at"]) > datetime.fromisoformat(
        body["tracked_at"]
    )
    assert_no_store(response)
    stored = await card_row(session, card.card_id)
    assert stored is not None
    assert (str(stored["stage"]), stored["version"]) == ("applied", 2)


async def test_ac26_a_move_may_go_to_any_stage_from_any_stage(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    """TA-2: the table is open. Withdrawn straight back to to_apply is one move."""
    account = await register(client, settings)
    card = await seed_card(
        session, settings, account, at=_earlier(clock), stage=ApplicationStage.WITHDRAWN
    )

    response = await client.put(
        card.stage_url, json={"stage": "to_apply", "version": 1}, headers=account.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["stage"] == "to_apply"
    assert response.json()["version"] == 2


async def test_t24_a_move_to_the_stage_it_already_stands_in_is_200_and_changes_nothing(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    before = await card_row(session, card.card_id)

    response = await client.put(
        card.stage_url, json={"stage": "to_apply", "version": 1}, headers=account.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    assert response.json()["stage"] == "to_apply"
    assert await card_row(session, card.card_id) == before


async def test_t20_a_stale_version_on_a_move_is_409_naming_the_current_one_and_writes_nothing(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    moved = await client.put(
        card.stage_url, json={"stage": "applied", "version": 1}, headers=account.headers
    )
    assert moved.status_code == 200, moved.text  # the card is now at version 2

    stale = await client.put(
        card.stage_url, json={"stage": "offer", "version": 1}, headers=account.headers
    )

    assert stale.status_code == 409, stale.text
    assert error_code(stale) == "tracked_application_version_conflict"
    assert error_body(stale)["current_version"] == 2
    stored = await card_row(session, card.card_id)
    assert stored is not None
    assert (str(stored["stage"]), stored["version"]) == ("applied", 2)


async def test_t20_a_version_ahead_of_the_card_is_409_too(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))

    response = await client.put(
        card.stage_url, json={"stage": "applied", "version": 9}, headers=account.headers
    )

    assert response.status_code == 409, response.text
    assert error_body(response)["current_version"] == 1


async def test_ac26_a_stale_version_is_refused_even_for_the_stage_the_card_already_has(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    """TA-4: the version guard runs before the no-op rule."""
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))

    response = await client.put(
        card.stage_url, json={"stage": "to_apply", "version": 5}, headers=account.headers
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "tracked_application_version_conflict"


@pytest.mark.parametrize(
    "body",
    [
        {"stage": "someday", "version": 1},
        {"stage": "applied", "version": 0},
        {"stage": "applied", "version": -1},
        {"stage": "applied"},
        {"version": 1},
        {"stage": "applied", "version": 1, "title": "x"},
    ],
    ids=[
        "unknown_stage",
        "version_zero",
        "version_negative",
        "no_version",
        "no_stage",
        "unknown_key",
    ],
)
async def test_t16_an_invalid_move_body_is_422_and_the_card_is_untouched(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    body: dict[str, object],
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    before = await card_row(session, card.card_id)

    response = await client.put(card.stage_url, json=body, headers=account.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    assert await card_row(session, card.card_id) == before


async def test_ac26_a_retitle_is_200_keeps_the_stage_and_its_instant_and_bumps_the_version(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(
        session, settings, account, at=_earlier(clock), stage=ApplicationStage.APPLIED
    )
    before = await card_row(session, card.card_id)
    assert before is not None

    response = await client.put(
        card.title_url,
        json={"title": "  Globex — Staff Engineer ", "version": 1},
        headers=account.headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == TRACKED_APPLICATION_KEYS
    assert body["title"] == "Globex — Staff Engineer"
    assert body["stage"] == "applied"
    assert body["version"] == 2
    assert datetime.fromisoformat(body["stage_changed_at"]) == before["stage_changed_at"]
    assert_no_store(response)
    stored = await card_row(session, card.card_id)
    assert stored is not None
    assert (stored["title"], stored["version"]) == ("Globex — Staff Engineer", 2)


async def test_ac26_a_null_title_clears_it(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock), title="Acme")

    response = await client.put(
        card.title_url, json={"title": None, "version": 1}, headers=account.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["title"] is None
    assert response.json()["version"] == 2


async def test_t24_the_same_title_with_the_current_version_is_200_and_the_version_stays(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock), title="Acme")
    before = await card_row(session, card.card_id)

    response = await client.put(
        card.title_url, json={"title": "Acme", "version": 1}, headers=account.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    assert response.json()["title"] == "Acme"
    assert await card_row(session, card.card_id) == before


async def test_t20_a_stale_version_on_a_retitle_is_409_and_the_title_is_unchanged(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock), title="Acme")
    first = await client.put(
        card.title_url, json={"title": "Globex", "version": 1}, headers=account.headers
    )
    assert first.status_code == 200, first.text

    stale = await client.put(
        card.title_url, json={"title": "Initech", "version": 1}, headers=account.headers
    )

    assert stale.status_code == 409, stale.text
    assert error_code(stale) == "tracked_application_version_conflict"
    assert error_body(stale)["current_version"] == 2
    stored = await card_row(session, card.card_id)
    assert stored is not None
    assert stored["title"] == "Globex"


@pytest.mark.parametrize(
    "body",
    [
        {"title": "x" * 121, "version": 1},
        {"title": "", "version": 1},
        {"title": "   ", "version": 1},
        {"title": "a\nb", "version": 1},
        {"title": "ok"},
        {"version": 1},
        {"title": "ok", "version": 0},
        {"title": "ok", "version": 1, "stage": "applied"},
    ],
    ids=[
        "over_120",
        "empty",
        "blank",
        "control_character",
        "no_version",
        "no_title_key_is_not_a_silent_clear",
        "version_zero",
        "unknown_key",
    ],
)
async def test_t16_an_invalid_retitle_body_is_422_and_the_card_is_untouched(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    body: dict[str, object],
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock), title="Acme")
    before = await card_row(session, card.card_id)

    response = await client.put(card.title_url, json=body, headers=account.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    assert await card_row(session, card.card_id) == before


async def test_t16_a_refused_retitle_never_echoes_the_title(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock), title="Acme")
    marker = f"QA31RETITLE-{uuid4().hex}"

    response = await client.put(
        card.title_url, json={"title": marker + "\nx", "version": 1}, headers=account.headers
    )

    assert response.status_code == 422, response.text
    assert marker not in response.text


@pytest.mark.parametrize("suffix", ["/stage", "/title", ""])
async def test_t16_a_malformed_card_id_in_the_path_is_422(
    client: AsyncClient, settings: Settings, suffix: str
) -> None:
    account = await register(client, settings)
    body = {"/stage": {"stage": "applied", "version": 1}, "/title": {"title": "x", "version": 1}}
    method = "DELETE" if suffix == "" else "PUT"

    response = await client.request(
        method, f"{ME_TRACKED}/not-a-uuid{suffix}", json=body.get(suffix), headers=account.headers
    )

    assert response.status_code == 422, response.text


async def test_t22_a_move_or_retitle_on_a_card_that_is_gone_is_404_tracked_application_not_found(
    client: AsyncClient, settings: Settings
) -> None:
    account = await register(client, settings)
    gone = uuid4()

    move = await client.put(
        f"{ME_TRACKED}/{gone}/stage",
        json={"stage": "applied", "version": 1},
        headers=account.headers,
    )
    retitle = await client.put(
        f"{ME_TRACKED}/{gone}/title", json={"title": "x", "version": 1}, headers=account.headers
    )

    for response in (move, retitle):
        assert response.status_code == 404, response.text
        assert error_code(response) == "tracked_application_not_found"


async def test_t25_a_card_whose_saved_cv_was_deleted_still_moves(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    await session.execute(
        text("DELETE FROM intake_base_cv WHERE id = :id"), {"id": card.entry.cv_id.value}
    )
    await session.commit()

    response = await client.put(
        card.stage_url, json={"stage": "applied", "version": 1}, headers=account.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["stage"] == "applied"


# ===================================================================================================
# DELETE …  (AC-26, AC-24's last sentence, T-30, T-31)
# ===================================================================================================


async def test_t30_untracking_is_204_with_no_body_and_removes_only_the_card(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    sibling = await seed_card(session, settings, account, at=_earlier(clock, 5))

    response = await client.delete(card.url, headers=account.headers)

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert_no_store(response)
    assert await card_row(session, card.card_id) is None
    assert await card_row(session, sibling.card_id) is not None


async def test_ac24_untracking_leaves_the_history_entry_its_documents_and_its_files_identical(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    """AC-24's last sentence. The run's whole row (documents included), its posting, its CV, its
    export job and the export file's **bytes** are read before and after."""
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock))
    card = await seed_card(session, settings, account, at=_earlier(clock), entry=entry)
    job = entry.jobs[0]
    export_file = settings.upload_dir / job.storage_ref.key
    assert export_file.exists(), "the export's bytes must be on the volume, or the proof is vacuous"

    async def snapshot() -> dict[str, object]:
        session.expire_all()
        out: dict[str, object] = {"file": export_file.read_bytes()}
        for table, key, value in (
            ("tailoring_run", "id", entry.run_id.value),
            ("posting_job_posting", "id", entry.posting_id.value),
            ("intake_base_cv", "id", entry.cv_id.value),
            ("export_job", "id", job.id.value),
        ):
            row = (
                (
                    await session.execute(
                        text(f"SELECT * FROM {table} WHERE {key} = :v"),  # noqa: S608 -- test-owned
                        {"v": value},
                    )
                )
                .mappings()
                .first()
            )
            assert row is not None, f"{table} row is missing"
            out[table] = dict(row)
        return out

    before = await snapshot()

    response = await client.delete(card.url, headers=account.headers)

    assert response.status_code == 204, response.text
    assert await card_row(session, card.card_id) is None
    assert await snapshot() == before


async def test_t31_untracking_twice_is_404_the_second_time(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    first = await client.delete(card.url, headers=account.headers)
    assert first.status_code == 204, first.text

    second = await client.delete(card.url, headers=account.headers)

    assert second.status_code == 404, second.text
    assert error_code(second) == "tracked_application_not_found"


async def test_ac26_a_card_can_be_tracked_again_after_it_was_untracked(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    removed = await client.delete(card.url, headers=account.headers)
    assert removed.status_code == 204, removed.text

    again = await client.post(
        ME_TRACKED, json={"tailoring_run_id": str(card.run_id)}, headers=account.headers
    )

    assert again.status_code == 201, again.text
    assert again.json()["id"] != str(card.card_id)
    assert again.json()["version"] == 1


# ===================================================================================================
# GET /api/me/board  (AC-30)
# ===================================================================================================


async def test_ac30_an_empty_board_is_200_with_an_empty_list(
    client: AsyncClient, settings: Settings
) -> None:
    account = await register(client, settings)

    response = await client.get(ME_BOARD, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}
    assert_no_store(response)


async def test_ac30_a_card_carries_its_fields_its_run_its_posting_and_its_cv(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    at = _earlier(clock)
    card = await seed_card(
        session, settings, account, at=at, stage=ApplicationStage.OFFER, title="Acme — SRE"
    )

    response = await client.get(ME_BOARD, headers=account.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items"}
    (item,) = body["items"]
    assert set(item) == BOARD_CARD_KEYS
    assert item["id"] == str(card.card_id)
    assert item["tailoring_run_id"] == str(card.run_id)
    assert item["stage"] == "offer"
    assert item["title"] == "Acme — SRE"
    assert item["version"] == 1
    assert set(item["run"]) == BOARD_RUN_KEYS
    assert datetime.fromisoformat(item["run"]["requested_at"]) == at
    assert item["run"]["edited"] is False
    assert set(item["posting"]) == BOARD_POSTING_KEYS
    assert item["posting"]["job_posting_id"] == str(card.entry.posting_id.value)
    assert item["posting"]["source"] == "pasted"
    assert item["posting"]["title"] is None
    assert item["posting"]["source_url"] is None
    assert item["posting"]["preview"] == card.entry.posting_preview
    assert len(item["posting"]["preview"]) <= 140
    assert set(item["base_cv"]) == BOARD_BASE_CV_KEYS
    assert item["base_cv"]["base_cv_id"] == str(card.entry.cv_id.value)
    assert item["base_cv"]["original_filename"] == "cv.pdf"
    assert item["base_cv"]["label"] is None


async def test_ac30_the_board_never_carries_a_document_body_or_the_full_posting_text(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    await seed_card(session, settings, account, at=_earlier(clock))

    response = await client.get(ME_BOARD, headers=account.headers)

    assert response.status_code == 200, response.text
    (item,) = response.json()["items"]
    forbidden = {
        "tailored_cv",
        "cover_letter",
        "edited_cv",
        "edited_cover_letter",
        "extracted_text",
        "text",
        "content",
    }
    assert forbidden.isdisjoint(item)
    assert forbidden.isdisjoint(item["posting"])
    assert forbidden.isdisjoint(item["base_cv"])
    assert "c" * 100 not in response.text, "a tailored document body is on the board"
    assert "word word word" not in response.text, "the extracted CV text is on the board"


async def test_ac30_the_board_is_ordered_by_stage_changed_at_then_id_both_descending(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    oldest = await seed_card(session, settings, account, at=_earlier(clock, 60))
    newest = await seed_card(session, settings, account, at=_earlier(clock, 1))
    tie_a = await seed_card(session, settings, account, at=_earlier(clock, 30))
    tie_b = await seed_card(session, settings, account, at=_earlier(clock, 30))
    tie_c = await seed_card(session, settings, account, at=_earlier(clock, 30))
    ties_by_id_desc = sorted(
        (tie_a.card_id, tie_b.card_id, tie_c.card_id), key=lambda card_id: card_id.int, reverse=True
    )

    response = await client.get(ME_BOARD, headers=account.headers)

    assert response.status_code == 200, response.text
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [
        str(newest.card_id),
        *(str(card_id) for card_id in ties_by_id_desc),
        str(oldest.card_id),
    ]


async def test_ac30_a_moved_card_rises_to_the_top(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    first = await seed_card(session, settings, account, at=_earlier(clock, 60))
    await seed_card(session, settings, account, at=_earlier(clock, 30))
    moved = await client.put(
        first.stage_url, json={"stage": "applied", "version": 1}, headers=account.headers
    )
    assert moved.status_code == 200, moved.text

    response = await client.get(ME_BOARD, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["id"] == str(first.card_id)
    assert response.json()["items"][0]["stage"] == "applied"


async def test_ac30_a_deleted_saved_cv_is_a_null_base_cv_and_the_card_is_still_listed(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    await session.execute(
        text("DELETE FROM intake_base_cv WHERE id = :id"), {"id": card.entry.cv_id.value}
    )
    await session.commit()

    response = await client.get(ME_BOARD, headers=account.headers)

    assert response.status_code == 200, response.text
    (item,) = response.json()["items"]
    assert item["id"] == str(card.card_id)
    assert item["base_cv"] is None
    assert item["run"] is not None
    assert item["posting"] is not None


async def test_ac30_a_card_whose_run_is_missing_is_listed_with_null_run_and_posting_and_warns(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-36. Still listed so it stays removable, with the `tracking.board_run_missing` warning
    naming the card's id (and nothing else)."""
    account = await register(client, settings)
    orphan = await seed_orphan_card(session, account, at=_earlier(clock))

    with caplog.at_level(logging.WARNING):
        response = await client.get(ME_BOARD, headers=account.headers)
        removal = await client.delete(f"{ME_TRACKED}/{orphan}", headers=account.headers)

    assert response.status_code == 200, response.text
    (item,) = response.json()["items"]
    assert item["id"] == str(orphan)
    assert item["run"] is None
    assert item["posting"] is None
    assert item["base_cv"] is None
    warnings = [
        r.getMessage() for r in caplog.records if "tracking.board_run_missing" in r.getMessage()
    ]
    assert len(warnings) == 1, warnings
    assert str(orphan) in warnings[0]
    assert removal.status_code == 204, removal.text
    assert await card_row(session, orphan) is None


async def test_ac30_the_run_reports_edited_once_a_document_was_revised(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    edited = await client.put(
        f"/api/me/tailoring-runs/{card.run_id}/documents/cv",
        json={"content": "Revised curriculum vitae line. " * 30, "expected_version": 3},
        headers=account.headers,
    )
    assert edited.status_code == 200, edited.text

    response = await client.get(ME_BOARD, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["run"]["edited"] is True


# ===================================================================================================
# AC-21: nothing else is routed
# ===================================================================================================


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", ME_BOARD),
        ("PUT", ME_BOARD),
        ("DELETE", ME_BOARD),
        ("GET", ME_TRACKED),
        ("PUT", ME_TRACKED),
        ("DELETE", ME_TRACKED),
        ("GET", f"{ME_TRACKED}/{uuid4()}"),
        ("POST", f"{ME_TRACKED}/{uuid4()}"),
        ("PATCH", f"{ME_TRACKED}/{uuid4()}"),
        ("GET", f"{ME_TRACKED}/{uuid4()}/stage"),
        ("POST", f"{ME_TRACKED}/{uuid4()}/stage"),
        ("DELETE", f"{ME_TRACKED}/{uuid4()}/stage"),
        ("GET", f"{ME_TRACKED}/{uuid4()}/title"),
        ("PATCH", f"{ME_TRACKED}/{uuid4()}/title"),
    ],
)
async def test_ac21_no_other_method_or_path_is_routed(
    client: AsyncClient, settings: Settings, method: str, path: str
) -> None:
    """Passes against the skeleton by construction (unrouted); it guards the contract's closed set."""
    account = await register(client, settings)

    response = await client.request(method, path, headers=account.headers)

    assert response.status_code in {404, 405}, response.text


async def test_ac21_the_openapi_document_lists_exactly_the_five_routes(app: FastAPI) -> None:
    paths = {
        path: sorted(method.upper() for method in item)
        for path, item in app.openapi()["paths"].items()
        if path.startswith(("/api/me/board", "/api/me/tracked-applications"))
    }

    assert paths == {
        "/api/me/board": ["GET"],
        "/api/me/tracked-applications": ["POST"],
        "/api/me/tracked-applications/{tracked_application_id}": ["DELETE"],
        "/api/me/tracked-applications/{tracked_application_id}/stage": ["PUT"],
        "/api/me/tracked-applications/{tracked_application_id}/title": ["PUT"],
    }


# ===================================================================================================
# T-26 / T-27: the tracking write limiter (600/h per user, fails OPEN)
# ===================================================================================================


async def test_t26_the_write_limiter_is_429_with_retry_after_and_the_refused_write_changes_nothing(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    override_settings(app, settings, tracking_write_rate_limit_per_hour=1)
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    first = await client.put(
        card.stage_url, json={"stage": "applied", "version": 1}, headers=account.headers
    )
    assert first.status_code == 200, first.text

    second = await client.put(
        card.stage_url, json={"stage": "offer", "version": 2}, headers=account.headers
    )

    assert second.status_code == 429, second.text
    assert error_code(second) == "rate_limited"
    assert "retry-after" in {name.lower() for name in second.headers}
    stored = await card_row(session, card.card_id)
    assert stored is not None
    assert (str(stored["stage"]), stored["version"]) == ("applied", 2)


async def test_t26_one_budget_is_shared_by_the_four_writes_and_is_per_user(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    override_settings(app, settings, tracking_write_rate_limit_per_hour=2)
    alice = await register(client, settings)
    bob = await register(client, settings)
    card = await seed_card(session, settings, alice, at=_earlier(clock))
    bobs = await seed_card(session, settings, bob, at=_earlier(clock))

    first = await client.put(
        card.stage_url, json={"stage": "applied", "version": 1}, headers=alice.headers
    )
    second = await client.put(
        card.title_url, json={"title": "Acme", "version": 2}, headers=alice.headers
    )
    third = await client.delete(card.url, headers=alice.headers)
    others = await client.delete(bobs.url, headers=bob.headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert third.status_code == 429, third.text
    assert error_code(third) == "rate_limited"
    assert others.status_code == 204, others.text
    assert await card_row(session, card.card_id) is not None


async def test_t26_the_board_read_is_not_limited(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    override_settings(app, settings, tracking_write_rate_limit_per_hour=1)
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    spent = await client.put(
        card.stage_url, json={"stage": "applied", "version": 1}, headers=account.headers
    )
    assert spent.status_code == 200, spent.text

    reads = [await client.get(ME_BOARD, headers=account.headers) for _ in range(3)]

    assert [r.status_code for r in reads] == [200, 200, 200]


async def test_t27_redis_down_the_write_proceeds_the_limiter_fails_open(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    tracked = await client.post(
        ME_TRACKED, json={"tailoring_run_id": str(entry.run_id.value)}, headers=account.headers
    )
    moved = await client.put(
        card.stage_url, json={"stage": "applied", "version": 1}, headers=account.headers
    )
    removed = await client.delete(card.url, headers=account.headers)

    assert tracked.status_code == 201, tracked.text
    assert moved.status_code == 200, moved.text
    assert removed.status_code == 204, removed.text


# ===================================================================================================
# T-28 / T-37: Postgres failures. The fault is injected BELOW the adapter's floor (the session), so
# the repositories' own translation and the handler's commit are what is under test.
# ===================================================================================================


async def test_t28_a_failed_commit_on_track_is_503_and_no_card(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=_earlier(clock), ready_formats=())

    async def _commit_fails() -> None:
        raise SQLAlchemyError("simulated commit failure (T-28)")

    monkeypatch.setattr(session, "commit", _commit_fails)

    response = await client.post(
        ME_TRACKED, json={"tailoring_run_id": str(entry.run_id.value)}, headers=account.headers
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    monkeypatch.undo()
    assert await card_count(session, account.user_id.value) == 0


async def test_t28_a_failed_commit_on_move_is_503_and_the_card_is_unchanged(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))
    before = await card_row(session, card.card_id)

    async def _commit_fails() -> None:
        raise SQLAlchemyError("simulated commit failure (T-28)")

    monkeypatch.setattr(session, "commit", _commit_fails)

    response = await client.put(
        card.stage_url, json={"stage": "applied", "version": 1}, headers=account.headers
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    monkeypatch.undo()
    assert await card_row(session, card.card_id) == before


async def test_t28_a_failed_commit_on_untrack_is_503_and_the_card_stays(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock))

    async def _commit_fails() -> None:
        raise SQLAlchemyError("simulated commit failure (T-28)")

    monkeypatch.setattr(session, "commit", _commit_fails)

    response = await client.delete(card.url, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    monkeypatch.undo()
    assert await card_row(session, card.card_id) is not None


async def test_t28_a_failed_commit_on_retitle_is_503_and_the_title_is_unchanged(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock), title="Acme")
    before = await card_row(session, card.card_id)

    async def _commit_fails() -> None:
        raise SQLAlchemyError("simulated commit failure (T-28)")

    monkeypatch.setattr(session, "commit", _commit_fails)

    response = await client.put(
        card.title_url, json={"title": "Globex", "version": 1}, headers=account.headers
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    monkeypatch.undo()
    assert await card_row(session, card.card_id) == before


async def test_t28_a_503_is_logged_with_the_error_type_and_never_the_title(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = await register(client, settings)
    card = await seed_card(session, settings, account, at=_earlier(clock), title="Acme")
    marker = f"QA31LOG-{uuid4().hex}"

    async def _commit_fails() -> None:
        raise SQLAlchemyError("simulated commit failure (T-28)")

    monkeypatch.setattr(session, "commit", _commit_fails)

    with caplog.at_level(logging.DEBUG):
        response = await client.put(
            card.title_url, json={"title": marker, "version": 1}, headers=account.headers
        )

    assert response.status_code == 503, response.text
    assert "db.request_failed" in caplog.text
    assert "SQLAlchemyError" in caplog.text
    assert marker not in caplog.text


async def test_t37_the_database_down_on_the_board_read_is_503(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await register(client, settings)
    await seed_card(session, settings, account, at=_earlier(clock))

    async def _read_fails(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated read failure (T-37)")

    monkeypatch.setattr(session, "execute", _read_fails)

    response = await client.get(ME_BOARD, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"


# ===================================================================================================
# A second user's data is never on this user's board (control for the authorization file)
# ===================================================================================================


async def test_ac30_the_board_lists_only_the_requesters_cards(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    alice = await register(client, settings)
    bob = await register(client, settings)
    mine = await seed_card(session, settings, alice, at=_earlier(clock))
    theirs = await seed_card(session, settings, bob, at=_earlier(clock))

    response = await client.get(ME_BOARD, headers=alice.headers)

    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(mine.card_id)}
    assert str(theirs.card_id) not in ids
