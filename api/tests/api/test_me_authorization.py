"""Who may reach what, over HTTP, on the account routes (slice 2.3, T21 RED; AC-22's HTTP half,
AC-24, AC-25, AC-26, AC-34, AC-35, H-8, H-9, H-31, H-52).

- **AC-24 / H-8 / H-9.** Every one of the twelve new routes depends on `require_user` only. The
  bearer refusals (missing, forged, a guest cookie alone) already pass against the skeleton —
  `require_user` is 2.1's real code — and each is paired here with the discriminating positive that
  cannot: a **valid** bearer whose account is gone must be 401 `not_signed_in`, which only a handler
  that resolves the user can answer.
- **AC-25.** 2.2's walker and AST scan already discover every module in `routers/`; the exception-set
  test there stays the proof. This file adds the positive control that the walker actually *sees*
  the twelve new routes and finds `require_user` — and nothing guest — on each. Green on arrival:
  the skeleton registered them with `RequireUserDep` alone, which is the property.
- **AC-26 / H-31 / H-36.** Users A and B and guest G each own a run with an export job (and its
  file) and a posting. A's bearer on B's and G's ids, on every id-taking route, is **404 with a body
  byte-identical to a nonexistent id's**, and B's and G's rows and files are untouched afterwards.
- **AC-22 (HTTP half).** A user-owned id on every guest route is 404 and creates nothing (green on
  arrival: the guest arms already compare owners — a regression guard for T11's equality).
- **AC-34.** `expires_at` is never null on a guest response (green on arrival: guest routes unedited
  but for the schema's type widening, which this guards).
- **AC-35 / H-52.** After `POST /api/auth/delete-account` the user's runs, postings, export jobs and
  export files are gone, the guest's work untouched, the still-valid token 401 `not_signed_in` on
  every new route, and the erasure line carries the widened counts.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.infrastructure.api.deps import (
    get_export_queue,
    get_tailoring_queue,
    require_guest_session,
    require_user,
    resolve_or_start_guest_session,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    A_PASSWORD,
    DELETE_ACCOUNT_URL,
    ME_EXPORT_JOBS,
    ME_POSTINGS,
    ME_RUNS,
    SAMPLE_CV,
    Account,
    Entry,
    count_rows,
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
from tests.integration.fakes import FakeExportQueue, FakeTailoringQueue

_REVISION = {"content": "Revised curriculum vitae line. " * 30, "expected_version": 3}


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every limiter these routes touch lives in Redis."""


@pytest.fixture(autouse=True)
def _queues(app: FastAPI) -> None:
    """No broker in this suite; a request that got as far as publishing must not reach one."""
    tailoring_queue, export_queue = FakeTailoringQueue(), FakeExportQueue()
    app.dependency_overrides[get_tailoring_queue] = lambda: tailoring_queue
    app.dependency_overrides[get_export_queue] = lambda: export_queue


@dataclass(frozen=True)
class _Route:
    method: str
    template: str
    body: dict[str, object] | None = None

    def url(
        self, entry: Entry | None, *, run_id: str | None = None, job_id: str | None = None
    ) -> str:
        run = run_id or (str(entry.run_id.value) if entry else str(uuid4()))
        job = job_id or (str(entry.jobs[0].id.value) if entry and entry.jobs else str(uuid4()))
        return self.template.format(run=run, job=job)

    @property
    def label(self) -> str:
        return f"{self.method} {self.template}"


_ROUTES = [
    _Route("POST", ME_POSTINGS, {"source": "pasted", "text": "We are hiring. " * 20}),
    _Route("GET", ME_POSTINGS),
    _Route("POST", ME_RUNS, {"base_cv_id": str(uuid4()), "job_posting_id": str(uuid4())}),
    _Route("GET", ME_RUNS),
    _Route("GET", ME_RUNS + "/{run}"),
    _Route("PUT", ME_RUNS + "/{run}/documents/cv", _REVISION),
    _Route("DELETE", ME_RUNS + "/{run}"),
    _Route("GET", ME_RUNS + "/{run}/documents/cv/download?format=md"),
    _Route("POST", ME_RUNS + "/{run}/exports", {"document": "cv", "format": "pdf"}),
    _Route("GET", ME_RUNS + "/{run}/exports"),
    _Route("GET", ME_EXPORT_JOBS + "/{job}"),
    _Route("GET", ME_EXPORT_JOBS + "/{job}/file"),
]
_ID_ROUTES = [route for route in _ROUTES if "{" in route.template]


async def _call(
    client: AsyncClient, route: _Route, url: str, headers: dict[str, str] | None = None
) -> Response:
    return await client.request(route.method, url, json=route.body, headers=headers or {})


# --- AC-24 / H-8 / H-9 -------------------------------------------------------------------------


@pytest.mark.parametrize("route", _ROUTES, ids=lambda r: r.label)
async def test_h8_no_bearer_is_401_invalid_access_token(client: AsyncClient, route: _Route) -> None:
    response = await _call(client, route, route.url(None))

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"
    assert "www-authenticate" in {name.lower() for name in response.headers}


@pytest.mark.parametrize("route", _ROUTES, ids=lambda r: r.label)
async def test_h8_a_forged_bearer_is_401_invalid_access_token(
    client: AsyncClient, route: _Route
) -> None:
    response = await _call(
        client, route, route.url(None), {"Authorization": "Bearer forged.token.x"}
    )

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"


@pytest.mark.parametrize("route", _ROUTES, ids=lambda r: r.label)
async def test_ac24_a_guest_cookie_alone_authorizes_nothing(
    client: AsyncClient, session: AsyncSession, route: _Route
) -> None:
    await mint_guest(client, session)  # tc_guest now rides on every request

    response = await _call(client, route, route.url(None))

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"


@pytest.mark.parametrize("route", _ROUTES, ids=lambda r: r.label)
async def test_h9_a_valid_bearer_whose_account_is_gone_is_401_not_signed_in(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    route: _Route,
) -> None:
    """The discriminating positive for the three refusals above: the token verifies, so only a
    handler that resolves the user can refuse — and nothing may be written."""
    account = await register(client, settings)
    entry = await seed_entry(
        session, settings, account.owner, at=clock.now() - timedelta(minutes=5)
    )
    await session.execute(
        text("DELETE FROM identity_user WHERE id = :id"), {"id": account.user_id.value}
    )

    response = await _call(client, route, route.url(entry), account.headers)

    assert response.status_code == 401, response.text
    assert error_code(response) == "not_signed_in"
    assert await count_rows(session, "tailoring_run", user_id=account.user_id.value) == 0
    assert await count_rows(session, "posting_job_posting", user_id=account.user_id.value) == 0


# --- AC-25: the walker sees the new routes, with require_user and nothing guest ---------------


def test_ac25_the_walker_finds_every_new_route_on_require_user_alone(app: FastAPI) -> None:
    expected = {
        f"{route.method} {route.template.split('?')[0]}".replace("{run}", "{tailoring_run_id}")
        .replace("{job}", "{export_job_id}")
        .replace("/documents/cv", "/documents/{kind}")
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


# --- AC-26 / H-31 / H-36: the A/B/G matrix ------------------------------------------------------


@dataclass
class _World:
    alice: Account
    bob: Account
    bob_entry: Entry
    guest_entry: Entry


async def _world(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> _World:
    alice = await register(client, settings)
    bob = await register(client, settings)
    guest = await mint_guest(client, session)
    at = clock.now() - timedelta(minutes=10)
    await seed_entry(session, settings, alice.owner, at=at)
    return _World(
        alice=alice,
        bob=bob,
        bob_entry=await seed_entry(session, settings, bob.owner, at=at),
        guest_entry=await seed_entry(session, settings, guest, at=at),
    )


async def _untouched(session: AsyncSession, settings: Settings, entry: Entry) -> None:
    assert await count_rows(session, "tailoring_run", id=entry.run_id.value) == 1
    assert await count_rows(session, "posting_job_posting", id=entry.posting_id.value) == 1
    for job in entry.jobs:
        assert await count_rows(session, "export_job", id=job.id.value) == 1
        assert (settings.upload_dir / job.storage_ref.key).exists()
    version = (
        await session.execute(
            text("SELECT version FROM tailoring_run WHERE id = :id"), {"id": entry.run_id.value}
        )
    ).scalar_one()
    assert version == entry.run_version, "the run was written"
    assert await count_rows(session, "export_job", tailoring_run_id=entry.run_id.value) == len(
        entry.jobs
    )


@pytest.mark.parametrize("whose", ["bob", "guest"])
@pytest.mark.parametrize("route", _ID_ROUTES, ids=lambda r: r.label)
async def test_ac26_another_owners_id_is_404_byte_identical_and_touches_nothing(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    route: _Route,
    whose: str,
) -> None:
    world = await _world(client, settings, session, clock)
    target = world.bob_entry if whose == "bob" else world.guest_entry

    theirs = await _call(client, route, route.url(target), world.alice.headers)
    nobodys = await _call(
        client,
        route,
        route.url(None, run_id=str(uuid4()), job_id=str(uuid4())),
        world.alice.headers,
    )

    assert theirs.status_code == 404, theirs.text
    assert nobodys.status_code == 404, nobodys.text
    assert theirs.content == nobodys.content
    assert error_code(theirs) in {"tailoring_run_not_found", "export_job_not_found"}
    await _untouched(session, settings, target)


async def test_ac26_the_history_lists_only_the_users_own_runs(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    world = await _world(client, settings, session, clock)

    response = await client.get(ME_RUNS, headers=world.alice.headers)

    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert len(ids) == 1
    assert str(world.bob_entry.run_id.value) not in ids
    assert str(world.guest_entry.run_id.value) not in ids


async def test_ac26_the_guest_list_never_lists_a_users_run(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    """Green on arrival (1.3's list keys on the session); the positive is the guest's own run."""
    world = await _world(client, settings, session, clock)

    response = await client.get("/api/tailoring-runs")

    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(world.guest_entry.run_id.value)}


# --- AC-22 (HTTP half): a user-owned id on every guest route ----------------------------------


async def test_ac22_a_user_owned_id_on_every_guest_route_is_404_and_creates_nothing(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(
        session, settings, account.owner, at=clock.now() - timedelta(minutes=5)
    )
    await mint_guest(client, session)
    # Read up front: a 404 rolls the shared session back, which expires every loaded aggregate.
    run, job = entry.run_id.value, entry.jobs[0].id.value
    cv_id, posting_id = str(entry.cv_id.value), str(entry.posting_id.value)
    runs_before = await count_rows(session, "tailoring_run")
    jobs_before = await count_rows(session, "export_job")

    responses = {
        "get run": await client.get(f"/api/tailoring-runs/{run}"),
        "put doc": await client.put(f"/api/tailoring-runs/{run}/documents/cv", json=_REVISION),
        "inline": await client.get(f"/api/tailoring-runs/{run}/documents/cv/download?format=md"),
        "export": await client.post(
            f"/api/tailoring-runs/{run}/exports", json={"document": "cv", "format": "docx"}
        ),
        "exports": await client.get(f"/api/tailoring-runs/{run}/exports"),
        "job": await client.get(f"/api/export-jobs/{job}"),
        "file": await client.get(f"/api/export-jobs/{job}/file"),
        "run with a saved cv": await client.post(
            "/api/tailoring-runs",
            json={"base_cv_id": cv_id, "job_posting_id": posting_id},
        ),
        "posting": await client.get(f"/api/job-postings/{posting_id}"),
    }

    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(responses, 404)
    assert await count_rows(session, "tailoring_run") == runs_before
    assert await count_rows(session, "export_job") == jobs_before


# --- AC-34: expires_at is never null on a guest response --------------------------------------


async def test_ac34_every_guest_response_shape_carries_an_expires_at(
    client: AsyncClient, session: AsyncSession, clock: FixedClock
) -> None:
    cv = await client.post("/api/base-cvs", files={"file": ("cv.txt", SAMPLE_CV, "text/plain")})
    assert cv.status_code == 201, cv.text
    posting = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": "We are hiring. " * 20}
    )
    run = await client.post(
        "/api/tailoring-runs",
        json={"base_cv_id": cv.json()["id"], "job_posting_id": posting.json()["id"]},
    )
    shapes = {
        "JobPostingResponse": posting,
        "JobPostingSummary": await client.get("/api/job-postings"),
        "TailoringRunResponse (POST)": run,
        "TailoringRunResponse (GET)": await client.get(f"/api/tailoring-runs/{run.json()['id']}"),
        "TailoringRunSummary": await client.get("/api/tailoring-runs"),
    }

    for name, response in shapes.items():
        assert response.status_code in (200, 201, 202), (name, response.text)
        body = response.json()
        items = body.get("items", [body])
        assert items, name
        for item in items:
            assert item["expires_at"] is not None, name


# --- AC-35 / H-52: account deletion takes the history ------------------------------------------


async def test_ac35_deleting_the_account_takes_its_history_and_files_and_spares_a_guest(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = await register(client, settings)
    at = clock.now() - timedelta(minutes=5)
    entry = await seed_entry(
        session, settings, account.owner, at=at, ready_formats=(ExportFormat.PDF, ExportFormat.DOCX)
    )
    guest = await mint_guest(client, session)
    guest_entry = await seed_entry(session, settings, guest, at=at)

    with caplog.at_level(logging.INFO):
        deleted = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": A_PASSWORD},
            headers={**account.headers, "Origin": settings.public_base_url},
        )

    assert deleted.status_code == 204, deleted.text
    for table in ("tailoring_run", "posting_job_posting", "export_job", "intake_base_cv"):
        assert await count_rows(session, table, user_id=account.user_id.value) == 0, table
    assert not any((settings.upload_dir / j.storage_ref.key).exists() for j in entry.jobs)
    assert await count_rows(session, "tailoring_run", id=guest_entry.run_id.value) == 1
    assert (settings.upload_dir / guest_entry.jobs[0].storage_ref.key).exists()
    (line,) = [
        json.loads(r.getMessage())
        for r in caplog.records
        if "retention.account_erased" in r.getMessage()
    ]
    counts = {
        key: line.get(key) for key in ("tailoring_runs", "job_postings", "export_jobs", "files")
    }
    # one run, one posting, two export jobs; files = one saved-CV key + two export keys
    assert counts == {"tailoring_runs": 1, "job_postings": 1, "export_jobs": 2, "files": 3}, line


@pytest.mark.parametrize("route", _ROUTES, ids=lambda r: r.label)
async def test_ac35_the_still_valid_token_is_401_not_signed_in_after_deletion(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock, route: _Route
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(
        session, settings, account.owner, at=clock.now() - timedelta(minutes=5)
    )
    deleted = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": A_PASSWORD},
        headers={**account.headers, "Origin": settings.public_base_url},
    )
    assert deleted.status_code == 204, deleted.text

    response = await _call(client, route, route.url(entry), account.headers)

    assert response.status_code == 401, response.text
    assert error_code(response) == "not_signed_in"
