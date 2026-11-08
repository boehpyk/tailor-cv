"""The guest boundary and the out-of-credits walk (slice 5.2, T28, **red first**): technical plan §0.7
and §4's last two rows; AC-14, AC-15, AC-16, AC-17 (the reads and edits that stay); F-25, F-26, F-27.

- **AC-15 / F-25.** `POST /api/tailoring-runs` answers 403 `account_required` to **any** request: with a
  live guest cookie, with none, with a bad body, with no body, with an expired session. It mints no
  session, touches no limiter (Redis stays empty), writes no row. Pinned structurally too: the handler
  takes no body parameter and no dependency, because that is what *"before the cookie or the body"*
  means in a framework that validates before it runs a handler.
- **AC-16 / F-26.** Every route of `routers/export.py` (discovered from the router, so a route added
  later is covered) answers 403 `account_required`, malformed ids included, through **one
  router-level dependency**, pinned on `router.dependencies`.
- **AC-14.** At balance 0 an account still reads its runs, edits them and creates and downloads every
  export format. This walk is green on arrival by design: it is the guard that no later change makes
  the ledger gate more than the one route that spends a credit.
- **AC-17.** A guest keeps reading and editing a run made before the release.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.value_objects import ExportJobId
from tailorcraft.infrastructure.api.deps import get_export_queue, refuse_guest_export
from tailorcraft.infrastructure.api.routers import export, tailoring
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import Settings
from tests.api.credits_support import ME_RUNS, reset_ledger
from tests.api.me_support import (
    ME_EXPORT_JOBS,
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
)
from tests.integration.fakes import FakeExportQueue

GUEST_RUNS = "/api/tailoring-runs"
A_BODY = {"base_cv_id": str(uuid4()), "job_posting_id": str(uuid4())}
_REVISED_CV = "Revised curriculum vitae line. " * 30


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Limiters live in Redis; AC-15's "touches no limiter" is read off an empty Redis."""


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


async def redis_keys(settings: Settings) -> list[str]:
    redis = create_redis(settings.redis_url)
    try:
        return sorted([str(k) async for k in redis.scan_iter(match="*")])
    finally:
        await redis.aclose()


def refused(response: Response) -> None:
    assert response.status_code == 403, response.text
    assert error_code(response) == "account_required"
    assert "set-cookie" not in response.headers, dict(response.headers)


async def guest_tables(session: AsyncSession) -> tuple[int, int]:
    return (
        await count_rows(session, "identity_guest_session"),
        await count_rows(session, "tailoring_run"),
    )


# ------------------------------------------------------------------------ AC-15, F-25, F-27


@pytest.mark.parametrize(
    "send",
    [
        pytest.param(lambda c: c.post(GUEST_RUNS, json=A_BODY), id="valid-body"),
        pytest.param(lambda c: c.post(GUEST_RUNS), id="no-body"),
        pytest.param(lambda c: c.post(GUEST_RUNS, json={"nonsense": True}), id="wrong-body"),
        pytest.param(lambda c: c.post(GUEST_RUNS, json=[1, 2]), id="array-body"),
        pytest.param(
            lambda c: c.post(
                GUEST_RUNS, content=b"{not json", headers={"content-type": "application/json"}
            ),
            id="malformed-json",
        ),
    ],
)
async def test_ac15_a_request_with_no_cookie_is_403_and_mints_nothing_limits_nothing_writes_nothing(
    client: AsyncClient, session: AsyncSession, settings: Settings, send: Any
) -> None:
    before = await guest_tables(session)

    response = await send(client)

    refused(response)
    assert await guest_tables(session) == before, "no session minted, no run written"
    assert await redis_keys(settings) == [], "no limiter was touched"


@pytest.mark.parametrize(
    "body", [A_BODY, None, {"nonsense": True}], ids=["valid-body", "no-body", "wrong-body"]
)
async def test_ac15_a_live_guest_cookie_changes_nothing(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    body: dict[str, Any] | None,
) -> None:
    await mint_guest(client, session)
    keys = await redis_keys(settings)
    before = await guest_tables(session)

    response = await client.post(GUEST_RUNS, json=body)

    refused(response)
    assert await guest_tables(session) == before
    assert await redis_keys(settings) == keys, "the upload's own limiter keys are the only ones"


async def test_f27_an_expired_guest_session_is_still_403_account_required_not_401(
    client: AsyncClient, session: AsyncSession
) -> None:
    guest = await mint_guest(client, session)
    await session.execute(
        text("UPDATE identity_guest_session SET expires_at = :t WHERE id = :i"),
        {"t": SystemClock().now() - timedelta(hours=1), "i": guest.guest_session_id.value},
    )
    await session.commit()

    response = await client.post(GUEST_RUNS, json=A_BODY)

    refused(response)


async def test_ac15_the_guest_handler_takes_no_body_and_no_dependency_so_nothing_runs_before_it(
    app: FastAPI,
) -> None:
    (route,) = [
        r
        for r in _iter_api_routes(app.routes)
        if r.path == GUEST_RUNS and "POST" in (r.methods or ())
    ]

    assert tailoring.router.dependencies == []
    assert route.dependant.dependencies == [], "a dependency would run (and may mint) first"
    assert route.dependant.body_params == [], "a body parameter is validated before the handler"
    assert route.dependant.query_params == []
    assert route.dependant.path_params == []
    assert route.dependant.header_params == []
    assert route.dependant.cookie_params == []


async def test_a_signed_in_account_on_the_guest_path_is_403_account_required_too(
    client: AsyncClient, settings: Settings
) -> None:
    account = await register(client, settings)

    response = await client.post(GUEST_RUNS, json=A_BODY, headers=account.headers)

    refused(response)


# --------------------------------------------------------------------------- AC-16, F-26


def _export_routes() -> list[APIRoute]:
    routes = [r for r in export.router.routes if isinstance(r, APIRoute)]
    assert len(routes) >= 5, "the discovery must find every export route"
    return routes


def test_ac16_the_export_router_carries_exactly_one_dependency_the_guest_refusal() -> None:
    assert [d.dependency for d in export.router.dependencies] == [refuse_guest_export]


def test_ac16_every_export_route_in_the_app_sits_behind_the_refusal(app: FastAPI) -> None:
    paths = {(r.path, m) for r in _export_routes() for m in (r.methods or ())}
    served = {
        (r.path, m): r
        for r in _iter_api_routes(app.routes)
        for m in (r.methods or ())
        if (r.path, m) in paths
    }
    assert set(served) == paths, "every export route is served by the app"

    unguarded = sorted(
        k
        for k, r in served.items()
        if refuse_guest_export not in _all_dependency_calls(r.dependant)
    )

    assert unguarded == []


def _fill(path: str, **values: str) -> str:
    out = path
    for name, value in values.items():
        out = out.replace("{" + name + "}", value)
    assert "{" not in out, out
    return out


@pytest.mark.parametrize("ids", ["malformed", "well-formed-unknown"])
@pytest.mark.parametrize("cookie", [False, True], ids=["no-cookie", "live-guest-cookie"])
async def test_ac16_every_export_route_answers_403_account_required_before_any_id_is_validated(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    ids: str,
    cookie: bool,
) -> None:
    queue = FakeExportQueue()
    app.dependency_overrides[get_export_queue] = lambda: queue
    if cookie:
        await mint_guest(client, session)
    run_id, job_id = (
        ("not-a-uuid", "also-not") if ids == "malformed" else (str(uuid4()), str(uuid4()))
    )
    jobs_before = await count_rows(session, "export_job")

    attempts: list[tuple[str, str, Response]] = []
    for route in _export_routes():
        url = _fill(route.path, run_id=run_id, export_job_id=job_id, kind="bogus-kind")
        for method in sorted(route.methods or ()):
            body = {"document": "nonsense"} if method == "POST" else None
            attempts.append((method, url, await client.request(method, url, json=body)))

    assert len(attempts) >= 5
    for method, url, response in attempts:
        assert response.status_code == 403, (method, url, response.text)
        assert error_code(response) == "account_required", (method, url)
        assert "set-cookie" not in response.headers
    assert queue.enqueued == []
    assert await count_rows(session, "export_job") == jobs_before


async def test_ac16_a_guests_own_real_run_and_ready_export_are_still_refused(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    queue = FakeExportQueue()
    app.dependency_overrides[get_export_queue] = lambda: queue
    guest = await mint_guest(client, session)
    entry = await seed_entry(
        session, settings, guest, at=SystemClock().now() - timedelta(minutes=10)
    )
    job_id = entry.jobs[0].id.value
    run = entry.run_id.value
    before = await count_rows(session, "export_job", tailoring_run_id=run)

    responses = [
        await client.get(f"{GUEST_RUNS}/{run}/documents/cv/download?format=md"),
        await client.post(f"{GUEST_RUNS}/{run}/exports", json={"document": "cv", "format": "docx"}),
        await client.get(f"{GUEST_RUNS}/{run}/exports"),
        await client.get(f"/api/export-jobs/{job_id}"),
        await client.get(f"/api/export-jobs/{job_id}/file"),
    ]

    for response in responses:
        refused(response)
        assert not response.content.startswith(b"%PDF"), "no bytes leak past the refusal"
    assert queue.enqueued == []
    assert await count_rows(session, "export_job", tailoring_run_id=run) == before


# ------------------------------------------------------------------ AC-17: what a guest keeps


async def test_ac17_a_guest_still_reads_and_edits_a_run_made_before_the_release(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    guest = await mint_guest(client, session)
    entry = await seed_entry(
        session, settings, guest, at=SystemClock().now() - timedelta(minutes=10), ready_formats=()
    )
    run = entry.run_id.value

    read = await client.get(f"{GUEST_RUNS}/{run}")
    edit = await client.put(
        f"{GUEST_RUNS}/{run}/documents/cv",
        json={"content": _REVISED_CV, "expected_version": entry.run_version},
    )

    assert read.status_code == 200, read.text
    assert edit.status_code == 200, edit.text


# --------------------------------------------------------------------------- AC-14: balance 0


async def test_ac14_at_balance_zero_an_account_still_reads_edits_and_exports_in_every_format(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    queue = FakeExportQueue()
    app.dependency_overrides[get_export_queue] = lambda: queue
    account = await register(client, settings)
    entry = await seed_entry(
        session, settings, account.owner, at=SystemClock().now() - timedelta(minutes=10)
    )
    await reset_ledger(session, account.user_id.value, charged=1)
    await session.commit()
    run, h = f"{ME_RUNS}/{entry.run_id.value}", account.headers
    ready = entry.jobs[0].id.value

    steps: list[tuple[str, Response]] = [
        ("GET history", await client.get(ME_RUNS, headers=h)),
        ("GET run", await client.get(run, headers=h)),
        (
            "PUT edit",
            await client.put(
                f"{run}/documents/cv",
                json={"content": _REVISED_CV, "expected_version": entry.run_version},
                headers=h,
            ),
        ),
        ("GET md", await client.get(f"{run}/documents/cv/download?format=md", headers=h)),
        ("GET txt", await client.get(f"{run}/documents/cv/download?format=txt", headers=h)),
        (
            "POST pdf",
            await client.post(
                f"{run}/exports", json={"document": "cv", "format": "pdf"}, headers=h
            ),
        ),
        (
            "POST docx",
            await client.post(
                f"{run}/exports", json={"document": "cover_letter", "format": "docx"}, headers=h
            ),
        ),
        ("GET exports", await client.get(f"{run}/exports", headers=h)),
        ("GET job", await client.get(f"{ME_EXPORT_JOBS}/{ready}", headers=h)),
        ("GET file", await client.get(f"{ME_EXPORT_JOBS}/{ready}/file", headers=h)),
    ]

    for name, response in steps:
        assert response.status_code in (200, 202), (name, response.status_code, response.text)
    assert len(queue.enqueued) == 2
    assert all(isinstance(job, ExportJobId) for job in queue.enqueued)
    balance = (
        await session.execute(
            text("SELECT COALESCE(SUM(credits), 0) FROM billing_credit_entry WHERE user_id = :u"),
            {"u": account.user_id.value},
        )
    ).scalar_one()
    assert balance == 0, "none of these routes spends or grants a credit"
