"""API tests for `/api/me/tailoring-runs` — request, history, re-open, edit (slice 2.3, T21 RED;
AC-29, AC-30, AC-31, H-5, H-14…H-20, H-22, H-25…H-30, H-32, H-33, H-35).

Every handler is a T20 skeleton; each test here is red on its first status assertion against the
skeleton's 500. Inputs and runs are seeded through the real repositories on the test's own session
(`tests/api/me_support.py`), so a test about `GET` does not depend on `POST` working.

- **H-22 goes through the worker's own composition root**, as 1.3's API tests do:
  `container._build_use_case(settings, session)` with `GeminiLlm` replaced by a `FakeLlm` on that
  module — `dependency_overrides` cannot reach the worker. The fake's call count is asserted **before**
  anything about its arguments (2.2's `/verify` lesson).
- **Faults are injected below each adapter's floor**: the request's own `session.commit` /
  `session.execute`, a Redis URL nothing listens on, a broker double that refuses the publish.
- **H-33** reuses 1.4's racing-writer technique (a second session holding a stale copy, the row
  deleted underneath it) rather than a patched repository.
- **`retryable` for `base_cv_deleted` is `false`** (H-22: "no Try again" — *Try again* would 404
  on the same CV). T20 left `_tailoring_handlers.is_retryable` provisionally `False`; this file is
  where the spec decides it.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.tailoring.errors import TailoringNotQueued
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredCv, TailoringRunId
from tailorcraft.infrastructure.api.deps import get_tailoring_queue
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container as tailoring_container
from tests.api.me_support import (
    DOCUMENT_COLUMNS,
    HISTORY_BASE_CV_KEYS,
    HISTORY_ENTRY_KEYS,
    HISTORY_POSTING_KEYS,
    ME_BASE_CVS,
    ME_RUNS,
    TAILORING_RUN_RESPONSE_KEYS,
    Account,
    assert_test_database,
    build_concurrent_app,
    captured_statements,
    count_rows,
    error_body,
    error_code,
    mint_guest,
    new_client,
    override_settings,
    register,
    seed_entry,
)
from tests.integration.fakes import FakeLlm, FakeTailoringQueue
from tests.integration.owners import (
    a_draft,
    extracted_cv,
    extraction_failed_cv,
    failed_run,
    pasted_posting,
    queued_run,
    running_run,
    succeeded_run,
)

_REVISED_CV = "Revised curriculum vitae line. " * 30


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The tailoring and save limiters live in Redis, which the rollback never reaches."""


def _install_queue(app: FastAPI, *, refuse: bool = False) -> FakeTailoringQueue:
    queue = FakeTailoringQueue(
        outcome=TailoringNotQueued("simulated broker refusal (H-5)") if refuse else None
    )
    app.dependency_overrides[get_tailoring_queue] = lambda: queue
    return queue


async def _inputs(session: AsyncSession, owner: Owner, clock: FixedClock) -> dict[str, str]:
    """A saved, extracted CV and a posting, both owned by `owner` — the request body for a run."""
    cv = extracted_cv(owner, clock.now())
    posting = pasted_posting(owner, clock.now())
    await SqlAlchemyBaseCvRepository(session).add(cv)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    await session.commit()
    return {"base_cv_id": str(cv.id.value), "job_posting_id": str(posting.id.value)}


async def _add_run(session: AsyncSession, run: TailoringRun) -> TailoringRun:
    """A real posting for `run.job_posting_id` first — since 2.3 /verify (reviewer MINOR #1),
    `SqlAlchemyTailoringRunRepository.add` takes it `FOR KEY SHARE` and, through the request route's
    composition root, refuses a run whose posting does not exist. Every seed in this file that is
    not deliberately building H-30's "posting already gone" state goes through this one place."""
    await SqlAlchemyJobPostingRepository(session).add(
        JobPosting.from_pasted_text(
            id=run.job_posting_id,
            owner=run.owner,
            text=JobPostingText("posting " * 40),
            created_at=run.requested_at,
        )
    )
    await SqlAlchemyTailoringRunRepository(session).add(run)
    await session.commit()
    return run


async def _add_orphan_run(session: AsyncSession, run: TailoringRun) -> TailoringRun:
    """H-30 needs a history entry whose posting is already gone — precisely the row
    `SqlAlchemyTailoringRunRepository.add`'s posting lock now refuses to create. Inserted with Core
    SQL directly against `tailoring_run_table`, bypassing the repository (and its lock) entirely:
    the only way left to build this state on purpose, the same technique
    `test_tailoring_run_repository.py`'s `_raw_insert` uses for the CHECK-constraint rows the
    aggregate itself cannot build. `run` is returned unchanged — only how it lands differs."""
    owner = run.owner
    documents = run.documents
    metrics = run.metrics
    await session.execute(
        tailoring_run_table.insert().values(
            id=run.id,
            guest_session_id=owner.guest_session_id if isinstance(owner, GuestOwner) else None,
            user_id=owner.user_id if isinstance(owner, UserOwner) else None,
            base_cv_id=run.base_cv_id,
            job_posting_id=run.job_posting_id,
            status=run.status,
            failure_reason=run.failure_reason,
            tailored_cv=documents.cv if documents is not None else None,
            cover_letter=documents.cover_letter if documents is not None else None,
            model_name=metrics.model if metrics is not None else None,
            prompt_version=metrics.prompt_version if metrics is not None else None,
            prompt_tokens=metrics.prompt_tokens if metrics is not None else None,
            completion_tokens=metrics.completion_tokens if metrics is not None else None,
            llm_duration_ms=metrics.duration_ms if metrics is not None else None,
            requested_at=run.requested_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            version=run.version,
        )
    )
    await session.commit()
    return run


# ===================================================================================================
# AC-29 — POST /api/me/tailoring-runs
# ===================================================================================================


async def test_a_request_is_202_queued_user_owned_with_location_and_one_enqueue(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    queue = _install_queue(app)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 202, response.text
    payload = response.json()
    assert set(payload) == TAILORING_RUN_RESPONSE_KEYS
    assert payload["status"] == "queued"
    assert payload["expires_at"] is None
    assert response.headers["location"] == f"{ME_RUNS}/{payload['id']}"
    assert response.headers.get("cache-control") == "no-store"
    assert queue.enqueued == [TailoringRunId(UUID(payload["id"]))]
    assert (
        await count_rows(
            session, "tailoring_run", id=UUID(payload["id"]), user_id=account.user_id.value
        )
        == 1
    )


@pytest.mark.parametrize("whose", ["other_user", "guest", "nonexistent"])
@pytest.mark.parametrize("which", ["base_cv", "job_posting"])
async def test_h14_an_input_that_is_not_the_users_is_404_and_creates_nothing(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    whose: str,
    which: str,
) -> None:
    queue = _install_queue(app)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)
    if whose == "nonexistent":
        body[f"{which}_id"] = str(uuid4())
    else:
        owner: Owner = (
            (await register(client, settings)).owner
            if whose == "other_user"
            else await mint_guest(client, session)
        )
        body[f"{which}_id"] = (await _inputs(session, owner, clock))[f"{which}_id"]

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == f"{which}_not_found"
    assert await count_rows(session, "tailoring_run", user_id=account.user_id.value) == 0
    assert queue.enqueued == []


async def test_h15_a_saved_cv_whose_extraction_failed_is_409(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    _install_queue(app)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)
    failed_cv = extraction_failed_cv(account.owner, clock.now())
    await SqlAlchemyBaseCvRepository(session).add(failed_cv)
    await session.commit()
    body["base_cv_id"] = str(failed_cv.id.value)

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "base_cv_not_extracted"
    assert await count_rows(session, "tailoring_run", user_id=account.user_id.value) == 0


async def test_h16_an_active_run_refuses_a_second_and_names_it(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    _install_queue(app)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)
    active_id = str((await _add_run(session, running_run(account.owner, clock.now()))).id.value)

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "tailoring_already_running"
    assert error_body(response)["active_tailoring_run_id"] == active_id
    assert await count_rows(session, "tailoring_run", user_id=account.user_id.value) == 1


async def test_h17_at_the_user_cap_the_request_is_409_pointing_at_history(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(app, settings, max_tailoring_runs_per_user=2)
    _install_queue(app)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)
    for _ in range(2):
        await _add_run(session, failed_run(account.owner, clock.now()))

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "too_many_tailoring_runs"
    assert "delete older entries" in str(error_body(response)["message"]).lower()
    assert await count_rows(session, "tailoring_run", user_id=account.user_id.value) == 2


async def test_h17_below_the_user_cap_the_request_is_accepted(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(app, settings, max_tailoring_runs_per_user=2)
    _install_queue(app)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)
    await _add_run(session, failed_run(account.owner, clock.now()))

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 202, response.text


async def test_h18_the_tailoring_limiter_is_per_user(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(app, settings, tailoring_rate_limit_per_hour=1)
    _install_queue(app)
    alice = await register(client, settings)
    bob = await register(client, settings)
    alice_body = await _inputs(session, alice.owner, clock)
    bob_body = await _inputs(session, bob.owner, clock)

    first = await client.post(ME_RUNS, json=alice_body, headers=alice.headers)
    second = await client.post(ME_RUNS, json=alice_body, headers=alice.headers)
    other = await client.post(ME_RUNS, json=bob_body, headers=bob.headers)

    assert first.status_code == 202, first.text
    assert second.status_code == 429, second.text
    assert error_code(second) == "rate_limited"
    assert "retry-after" in {name.lower() for name in second.headers}
    assert other.status_code == 202, other.text


async def test_h18_the_tailoring_limiter_is_also_per_ip(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(
        app, settings, tailoring_rate_limit_per_hour=100, tailoring_rate_limit_per_ip_per_hour=1
    )
    _install_queue(app)
    alice = await register(client, settings)
    bob = await register(client, settings)
    alice_body = await _inputs(session, alice.owner, clock)
    bob_body = await _inputs(session, bob.owner, clock)

    first = await client.post(ME_RUNS, json=alice_body, headers=alice.headers)
    second = await client.post(ME_RUNS, json=bob_body, headers=bob.headers)

    assert first.status_code == 202, first.text
    assert second.status_code == 429, second.text
    assert error_code(second) == "rate_limited"


async def test_h19_redis_down_fails_closed_with_no_row_and_no_enqueue(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    queue = _install_queue(app)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "rate_limit_unavailable"
    assert await count_rows(session, "tailoring_run", user_id=account.user_id.value) == 0
    assert queue.enqueued == []


async def test_h20_a_failed_commit_is_503_with_no_row_and_no_enqueue(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _install_queue(app)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)

    async def _commit_fails() -> None:
        raise SQLAlchemyError("simulated commit failure (H-20)")

    monkeypatch.setattr(session, "commit", _commit_fails)

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    assert queue.enqueued == []
    assert await count_rows(session, "tailoring_run", user_id=account.user_id.value) == 0


async def test_h5_a_refused_enqueue_is_503_and_the_run_is_recorded_failed_not_queued(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    _install_queue(app, refuse=True)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)

    response = await client.post(ME_RUNS, json=body, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "queue_unavailable"
    rows = (
        await session.execute(
            text("SELECT id, status, failure_reason FROM tailoring_run WHERE user_id = :u"),
            {"u": account.user_id.value},
        )
    ).all()
    assert [(row.status, row.failure_reason) for row in rows] == [("failed", "not_queued")]
    reopened = await client.get(f"{ME_RUNS}/{rows[0].id}", headers=account.headers)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["retryable"] is True


# ===================================================================================================
# H-22 — the saved CV is deleted after the request, before the worker reads it
# ===================================================================================================


async def test_h22_a_deleted_saved_cv_fails_the_run_base_cv_deleted_with_no_paid_call(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_queue(app)
    fake_llm = FakeLlm(a_draft())
    monkeypatch.setattr(tailoring_container, "GeminiLlm", lambda settings: fake_llm)
    account = await register(client, settings)
    body = await _inputs(session, account.owner, clock)

    requested = await client.post(ME_RUNS, json=body, headers=account.headers)
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["id"]
    deleted = await client.delete(f"{ME_BASE_CVS}/{body['base_cv_id']}", headers=account.headers)
    assert deleted.status_code == 204, deleted.text

    execute = tailoring_container._build_use_case(settings, session)
    outcome = await execute(
        ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(UUID(run_id)))
    )

    assert fake_llm.calls == [], "no paid call for a run whose CV is gone"
    assert outcome is ExecuteTailoringRunOutcome.FAILED
    reopened = await client.get(f"{ME_RUNS}/{run_id}", headers=account.headers)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "failed"
    assert reopened.json()["failure_reason"] == "base_cv_deleted"
    assert reopened.json()["retryable"] is False
    history = await client.get(ME_RUNS, headers=account.headers)
    assert history.status_code == 200, history.text
    (entry,) = history.json()["items"]
    assert entry["failure_reason"] == "base_cv_deleted"
    assert entry["retryable"] is False
    assert entry["base_cv"] is None
    assert entry["base_cv_id"] == body["base_cv_id"]


# ===================================================================================================
# AC-30 — GET /api/me/tailoring-runs (history)
# ===================================================================================================


async def _seed_runs(
    session: AsyncSession,
    owner: Owner,
    clock: FixedClock,
    count: int,
    *,
    same_second_every: int = 3,
) -> list[TailoringRunId]:
    """`count` succeeded runs, newest first in the returned list; every `same_second_every` runs
    share one whole second, so page boundaries fall inside ties."""
    keyed: list[tuple[datetime, UUID, TailoringRunId]] = []
    for index in range(count):
        at = clock.now() - timedelta(seconds=index // same_second_every)
        run = await _add_run(session, succeeded_run(owner, at))
        keyed.append((at, run.id.value, run.id))
    # newest first: requested_at desc, then id desc (PostgreSQL's uuid order is UUID's int order)
    keyed.sort(key=lambda key: (key[0], key[1]), reverse=True)
    return [run_id for _, _, run_id in keyed]


async def test_h25_an_empty_history_is_an_empty_page(
    client: AsyncClient, settings: Settings
) -> None:
    account = await register(client, settings)

    response = await client.get(ME_RUNS, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}
    assert response.headers.get("cache-control") == "no-store"


async def test_the_default_page_is_twenty_with_a_cursor_and_pinned_keys(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    await _seed_runs(session, account.owner, clock, 21)

    response = await client.get(ME_RUNS, headers=account.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "next_cursor"}
    assert len(body["items"]) == 20
    assert isinstance(body["next_cursor"], str)
    for item in body["items"]:
        assert set(item) == HISTORY_ENTRY_KEYS
        assert "tailored_cv" not in item
        assert "cover_letter" not in item


async def test_walking_a_45_entry_history_returns_each_run_once_in_order(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    expected = await _seed_runs(session, account.owner, clock, 45)

    walked: list[str] = []
    sizes: list[int] = []
    cursor: str | None = None
    for _ in range(10):
        url = f"{ME_RUNS}?limit=20" + (f"&cursor={cursor}" if cursor else "")
        response = await client.get(url, headers=account.headers)
        assert response.status_code == 200, response.text
        page = response.json()
        walked.extend(item["id"] for item in page["items"])
        sizes.append(len(page["items"]))
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert sizes == [20, 20, 5]
    assert walked == [str(run_id.value) for run_id in expected]


@pytest.mark.parametrize("limit", ["0", "51", "abc"])
async def test_h27_a_limit_outside_1_to_50_is_422_validation_error(
    client: AsyncClient, settings: Settings, limit: str
) -> None:
    account = await register(client, settings)

    response = await client.get(f"{ME_RUNS}?limit={limit}", headers=account.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


async def test_h27_limit_fifty_is_accepted(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    await _seed_runs(session, account.owner, clock, 2)

    response = await client.get(f"{ME_RUNS}?limit=50", headers=account.headers)

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 2


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


_BAD_CURSORS = [
    pytest.param("not-base64!!", id="not-base64"),
    pytest.param(_b64("no-dot-here"), id="no-separator"),
    pytest.param(_b64("1790000000.not-a-uuid"), id="bad-uuid"),
    pytest.param(_b64(f"yesterday.{uuid4()}"), id="bad-seconds"),
    pytest.param(_b64(f"1790000000.5.{uuid4()}"), id="extra-part"),
    pytest.param(_b64(f"-1.{uuid4()}"), id="negative"),
]


@pytest.mark.parametrize("cursor", _BAD_CURSORS)
async def test_h26_a_malformed_or_tampered_cursor_is_422_invalid_cursor_and_never_echoed(
    client: AsyncClient, settings: Settings, cursor: str
) -> None:
    account = await register(client, settings)

    response = await client.get(f"{ME_RUNS}?cursor={cursor}", headers=account.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "invalid_cursor"
    assert cursor not in response.text


async def test_h26_a_well_formed_cursor_naming_nobodys_run_pages_the_users_own_history(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    """The cursor is unsigned on purpose (ADR-0024): a forged one can only move a user around
    their own history. A cursor in the future returns the user's newest entries."""
    account = await register(client, settings)
    expected = await _seed_runs(session, account.owner, clock, 2)
    forged = _b64(f"{int((clock.now() + timedelta(days=1)).timestamp())}.{uuid4()}")

    response = await client.get(f"{ME_RUNS}?cursor={forged}", headers=account.headers)

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(i.value) for i in expected]


async def test_h28_a_cursor_whose_run_was_deleted_continues_correctly(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    expected = await _seed_runs(session, account.owner, clock, 5)
    first = await client.get(f"{ME_RUNS}?limit=2", headers=account.headers)
    assert first.status_code == 200, first.text
    await session.execute(
        tailoring_run_table.delete().where(tailoring_run_table.c.id == expected[1])
    )
    await session.commit()

    second = await client.get(
        f"{ME_RUNS}?limit=2&cursor={first.json()['next_cursor']}", headers=account.headers
    )

    assert second.status_code == 200, second.text
    assert [item["id"] for item in second.json()["items"]] == [str(i.value) for i in expected[2:4]]


async def test_h29_h30_an_entry_with_its_cv_or_posting_gone_is_still_listed(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=clock.now(), ready_formats=())
    orphan = await _add_orphan_run(
        session, succeeded_run(account.owner, clock.now() - timedelta(minutes=1))
    )
    await SqlAlchemyBaseCvRepository(session).remove(entry.cv_id, account.owner)
    await session.commit()

    response = await client.get(ME_RUNS, headers=account.headers)

    assert response.status_code == 200, response.text
    by_id = {item["id"]: item for item in response.json()["items"]}
    kept = by_id[str(entry.run_id.value)]
    assert kept["base_cv"] is None
    assert kept["base_cv_id"] == str(entry.cv_id.value)
    assert kept["posting"] is not None
    assert set(kept["posting"]) == HISTORY_POSTING_KEYS
    assert by_id[str(orphan.id.value)]["posting"] is None


async def test_a_history_entry_names_its_cv_and_posting(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    entry = await seed_entry(session, settings, account.owner, at=clock.now(), ready_formats=())

    response = await client.get(ME_RUNS, headers=account.headers)

    assert response.status_code == 200, response.text
    (item,) = response.json()["items"]
    assert item["id"] == str(entry.run_id.value)
    assert item["status"] == "succeeded"
    assert item["edited"] is False
    assert item["retryable"] is False
    assert set(item["base_cv"]) == HISTORY_BASE_CV_KEYS
    assert item["base_cv"]["id"] == str(entry.cv_id.value)
    assert item["posting"]["id"] == str(entry.posting_id.value)
    assert item["posting"]["preview"] == entry.posting_preview


async def test_the_history_statement_selects_no_document_column(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    """AC-30 / AC-55, over HTTP: every statement the request sends names no document column, and
    the posting preview is computed in SQL."""
    account = await register(client, settings)
    await seed_entry(session, settings, account.owner, at=clock.now(), ready_formats=())

    with captured_statements(session) as statements:
        response = await client.get(ME_RUNS, headers=account.headers)

    assert response.status_code == 200, response.text
    reads = [
        s for s in statements if "tailoring_run" in s and s.lstrip().upper().startswith("SELECT")
    ]
    assert reads, "the request must have read the history"
    for statement in reads:
        select_list = statement.split(" FROM ", 1)[0]
        assert DOCUMENT_COLUMNS.findall(select_list) == []
        assert select_list.count("p.text") == select_list.count("left(p.text")


async def test_h35_the_database_down_on_the_list_is_503(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await register(client, settings)

    async def _read_fails(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated read failure (H-35)")

    monkeypatch.setattr(session, "execute", _read_fails)

    response = await client.get(ME_RUNS, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"


# ===================================================================================================
# AC-31 — GET one and PUT a document
# ===================================================================================================


async def _edited_entry(
    session: AsyncSession, settings: Settings, account: Account, clock: FixedClock
) -> TailoringRun:
    entry = await seed_entry(
        session, settings, account.owner, at=clock.now() - timedelta(minutes=5), ready_formats=()
    )
    run = entry.run
    run.revise_cv(TailoredCv(_REVISED_CV), expected_version=run.version, at=clock.now())
    await SqlAlchemyTailoringRunRepository(session).save(run)
    await session.commit()
    return run


async def test_get_one_returns_the_current_documents_and_null_expires_at(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    run = await _edited_entry(session, settings, account, clock)

    response = await client.get(f"{ME_RUNS}/{run.id.value}", headers=account.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == TAILORING_RUN_RESPONSE_KEYS
    assert body["tailored_cv"] == TailoredCv(_REVISED_CV).value
    assert body["version"] == run.version
    assert body["tailored_cv_edited_at"] is not None
    assert body["expires_at"] is None
    assert response.headers.get("cache-control") == "no-store"


async def test_h35_the_database_down_on_get_one_is_503(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await register(client, settings)
    run = await _add_run(session, succeeded_run(account.owner, clock.now()))

    async def _read_fails(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated read failure (H-35)")

    monkeypatch.setattr(session, "execute", _read_fails)
    session.expunge_all()

    response = await client.get(f"{ME_RUNS}/{run.id.value}", headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"


async def _put(
    client: AsyncClient,
    account: Account,
    run: TailoringRun,
    version: int,
    content: str = _REVISED_CV,
) -> Response:
    return await client.put(
        f"{ME_RUNS}/{run.id.value}/documents/cv",
        json={"content": content, "expected_version": version},
        headers=account.headers,
    )


async def test_put_revises_the_document_and_bumps_the_version(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    run = await _add_run(session, succeeded_run(account.owner, clock.now() - timedelta(minutes=1)))
    version = run.version

    response = await _put(client, account, run, version)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == TAILORING_RUN_RESPONSE_KEYS
    assert body["tailored_cv"] == TailoredCv(_REVISED_CV).value
    assert body["version"] == version + 1
    assert body["expires_at"] is None


async def test_h32_a_stale_edit_is_409_with_the_current_version_and_writes_nothing(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    run = await _add_run(session, succeeded_run(account.owner, clock.now() - timedelta(minutes=1)))
    current, run_uuid = run.version, run.id.value

    response = await _put(client, account, run, current - 1)

    assert response.status_code == 409, response.text
    assert error_code(response) == "document_version_conflict"
    assert error_body(response)["current_version"] == current
    stored = await count_rows(session, "tailoring_run", id=run_uuid, version=current)
    assert stored == 1


async def test_put_on_a_queued_run_is_409_not_editable_with_its_status(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    run = await _add_run(session, queued_run(account.owner, clock.now()))

    response = await _put(client, account, run, run.version)

    assert response.status_code == 409, response.text
    assert error_code(response) == "tailoring_run_not_editable"
    assert error_body(response)["status"] == "queued"


async def test_put_with_content_that_is_too_short_is_422_document_invalid_without_echo(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    run = await _add_run(session, succeeded_run(account.owner, clock.now() - timedelta(minutes=1)))
    marker = "QA_SHORT_REVISION_MARKER"

    response = await _put(client, account, run, run.version, content=marker)

    assert response.status_code == 422, response.text
    assert error_code(response) == "document_invalid"
    assert error_body(response)["problem"] == "too_short"
    assert marker not in response.text


async def test_the_save_limiter_is_per_user(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(app, settings, tailoring_revise_rate_limit_per_hour=1)
    alice = await register(client, settings)
    bob = await register(client, settings)
    alice_run = await _add_run(
        session, succeeded_run(alice.owner, clock.now() - timedelta(minutes=1))
    )
    bob_run = await _add_run(session, succeeded_run(bob.owner, clock.now() - timedelta(minutes=1)))

    first = await _put(client, alice, alice_run, alice_run.version)
    second = await _put(client, alice, alice_run, alice_run.version + 1)
    other = await _put(client, bob, bob_run, bob_run.version)

    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert error_code(second) == "rate_limited"
    assert other.status_code == 200, other.text


async def test_the_save_limiter_fails_open(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    run = await _add_run(session, succeeded_run(account.owner, clock.now() - timedelta(minutes=1)))
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await _put(client, account, run, run.version)

    assert response.status_code == 200, response.text


@pytest.fixture
def concurrent_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    """A real session per request — H-33's race needs a delete that is genuinely committed on
    another connection while the edit request is between its read and its write."""
    return build_concurrent_app(settings, engine, password_hasher)


async def test_h33_an_edit_racing_a_committed_deletion_is_409_null_version_then_404(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """H-33 as its row reads: the edit request has **already read** the run (`GetTailoringRun`)
    when the entry's deletion commits on another connection; the edit's `UPDATE … WHERE version`
    then matches no row → 409 `document_version_conflict` with `current_version: null`, nothing is
    written (the row stays gone), and *Load latest* is 404.

    The deletion is scheduled at the repository's `save` — after the read, before the flush —
    by a wrapper that commits a Core `DELETE` on a separate connection and then delegates to the
    real `save` unchanged. Real, committed rows on `concurrent_app`; the user is deleted at the
    end, and the cascades take the rest."""
    assert_test_database(settings)
    async with new_client(concurrent_app) as setup:
        account = await register(setup, settings)
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        run = succeeded_run(account.owner, clock.now() - timedelta(minutes=1))
        # A real posting for `run.job_posting_id` first (2.3 /verify, reviewer MINOR #1): `add`
        # now takes it `FOR KEY SHARE` and refuses a run whose posting does not exist.
        await SqlAlchemyJobPostingRepository(seeding).add(
            JobPosting.from_pasted_text(
                id=run.job_posting_id,
                owner=run.owner,
                text=JobPostingText("posting " * 40),
                created_at=run.requested_at,
            )
        )
        await SqlAlchemyTailoringRunRepository(seeding).add(run)
        await seeding.commit()
    run_id, version = run.id, run.version

    original_save = SqlAlchemyTailoringRunRepository.save
    deleted_mid_request: list[bool] = []

    async def _save_after_a_committed_delete(
        self: SqlAlchemyTailoringRunRepository, target: TailoringRun
    ) -> None:
        async with engine.begin() as other_connection:
            gone = await other_connection.execute(
                tailoring_run_table.delete().where(tailoring_run_table.c.id == run_id)
            )
            deleted_mid_request.append(gone.rowcount == 1)
        await original_save(self, target)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(SqlAlchemyTailoringRunRepository, "save", _save_after_a_committed_delete)
            async with new_client(concurrent_app) as editor:
                edit = await editor.put(
                    f"{ME_RUNS}/{run_id.value}/documents/cv",
                    json={"content": _REVISED_CV, "expected_version": version},
                    headers=account.headers,
                )

        assert deleted_mid_request == [True], "the delete must land between the read and the write"
        assert edit.status_code == 409, edit.text
        assert error_code(edit) == "document_version_conflict"
        assert error_body(edit)["current_version"] is None
        async with engine.connect() as reader:
            still_gone = await reader.execute(
                text("SELECT count(*) FROM tailoring_run WHERE id = :id"), {"id": run_id.value}
            )
        assert still_gone.scalar_one() == 0, "the losing edit must not write the row back"
        async with new_client(concurrent_app) as reloader:
            reload = await reloader.get(f"{ME_RUNS}/{run_id.value}", headers=account.headers)
        assert reload.status_code == 404, reload.text
        assert error_code(reload) == "tailoring_run_not_found"
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_user WHERE id = :id"), {"id": account.user_id.value}
            )


async def test_an_edit_after_the_entry_was_deleted_is_404(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    """H-33's tail on its own: once the deletion has committed, a fresh edit reads nothing — 404
    `tailoring_run_not_found`, never a 409."""
    account = await register(client, settings)
    run = await _add_run(session, succeeded_run(account.owner, clock.now() - timedelta(minutes=1)))
    run_uuid, version = run.id.value, run.version
    deleted = await client.delete(f"{ME_RUNS}/{run_uuid}", headers=account.headers)
    assert deleted.status_code == 204, deleted.text

    edit = await client.put(
        f"{ME_RUNS}/{run_uuid}/documents/cv",
        json={"content": _REVISED_CV, "expected_version": version},
        headers=account.headers,
    )

    assert edit.status_code == 404, edit.text
    assert error_code(edit) == "tailoring_run_not_found"
