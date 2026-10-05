"""AC-42 (and AC-29 over its flow, AC-41's runtime half) — the planted-marker test for the
application board (slice 3.1, T22 RED).

**Hard from the start.** Every step asserts its own exact status, in the spec's order; the fake LLM's
**call count is asserted before its arguments** (inside `_work_a_run`); nothing is gated on a
previous step having worked (2.2's `/verify` MAJOR: a soft test that can skip its own flow passes
green without its claim). Against the T20 skeleton the flow is therefore red at its first board step
— `POST /api/me/tracked-applications`, `assert 500 == 201` — which is the honest red: every later
marker is planted only once T23 makes each step real. **No soft gate is used, so there is nothing for
the post-T23 commit to remove.**

**The flow** (the spec's): register → saved CV (marker filename, text, label) → pasted posting and a
fetched one (marker title and URL, a fake fetcher) → run (fake LLM, marker documents) → track with a
marker title → move x3 → a refused (422) over-long marker title → retitle → board → untrack → track
again → delete the saved CV → board → delete the history entry → a second saved CV, a second run on
the fetched posting, tracked with a marker title → delete the account.

**What "no marker" covers**: every captured log record (stdlib and structlog, every logger — `caplog`
at DEBUG on the root), which includes `LoggingEventPublisher`'s rendering of every domain event the
API publishes; every domain event, field by field (a tee publisher on `get_event_publisher`); every
Sentry envelope (initialised through the real `configure_sentry`, a probe proving the channel is
live); every Redis key. **Positive controls**: after each tracking step a log line names the card's
`tracked_application_id`, and the erasure line names the `user_id` — so the capture is proven live
rather than assumed — and the tee holds the events the flow must have produced (3 tracked, 3 stage
changes, 1 untracked).

**AC-29 rides along**: after every board write, no card points at a run another user owns or at a run
that does not exist — with a positive control (cards looked at).

**AC-41's runtime half**: the tracking steps enqueue nothing and call no LLM — the fake LLM is called
exactly once per *run* (inside `_work_a_run`), the tailoring queue was asked for exactly the two runs,
the export queue for none.

**The second test** drives `erase-account` on a user whose card title is a marker, through the real
command against committed rows (those composition roots open their own engine and cannot see the
rolled-back test transaction): no title in any log record, the user's id and the card count present
(green on arrival — `EraseAccount` landed at T17; this pins it over the card title).

Spec ambiguity resolved: AC-42 lists the "event publisher's rendering" and "domain event field" as
two channels; both are covered (`caplog` of the real `LoggingEventPublisher`, and the tee's fields).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sentry_sdk
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tracking.events import (
    ApplicationStageChanged,
    ApplicationTracked,
    ApplicationUntracked,
)
from tailorcraft.infrastructure.api.deps import (
    get_event_publisher,
    get_export_queue,
    get_job_posting_fetcher,
    get_tailoring_queue,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention import erase_account_command
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    A_PASSWORD,
    DELETE_ACCOUNT_URL,
    ME_BASE_CVS,
    ME_POSTINGS,
    ME_RUNS,
    Account,
    assert_test_database,
    bearer,
    new_client,
    seed_user_and_sign_in,
)
from tests.api.test_history_privacy_markers import (
    _draft,
    _event_text,
    _MarkerFetcher,
    _TeePublisher,
    _work_a_run,
    sentry_envelopes,  # noqa: F401 -- a fixture, used by name below
)
from tests.api.tracking_support import (
    ME_BOARD,
    ME_TRACKED,
    assert_no_card_crosses_owners,
    card_count_on,
    seed_card,
)
from tests.integration.fakes import FakeExportQueue, FakeTailoringQueue
from tests.integration.tracking.support import seed_user

_PREFIX = "QA42MARKER"


def _marker(label: str) -> str:
    return f"{_PREFIX}-{label}-{uuid4().hex}"


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every write passes a limiter that lives in Redis; the rollback never reaches it."""


async def test_ac42_no_marker_leaks_across_the_whole_board_flow(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
    sentry_envelopes: list[str],  # noqa: F811 -- the imported fixture, requested by name
) -> None:
    email = f"{_marker('email').lower()}@example.com"
    filename = _marker("filename") + ".txt"
    second_filename = _marker("second-filename") + ".txt"
    label = _marker("label")
    cv_text = " ".join([_marker("cv-text")] + ["experienced platform engineer."] * 30)
    pasted_text = _marker("pasted-posting") + " " + "we are hiring a platform engineer. " * 10
    fetched_title = _marker("fetched-title")
    fetched_url = f"https://jobs.example.com/{_marker('fetched-url')}"
    fetched_text = "Senior platform engineer, remote, fully async team. " * 6
    run_cv, run_letter = _marker("tailored-cv"), _marker("cover-letter")
    second_cv, second_letter = _marker("second-cv"), _marker("second-letter")
    title_one, title_two = _marker("card-title"), _marker("card-retitle")
    refused_title = _marker("card-title-refused")
    title_three = _marker("card-title-second-run")

    publisher = _TeePublisher()
    app.dependency_overrides[get_event_publisher] = lambda: publisher
    tailoring_queue, export_queue = FakeTailoringQueue(), FakeExportQueue()
    app.dependency_overrides[get_tailoring_queue] = lambda: tailoring_queue
    app.dependency_overrides[get_export_queue] = lambda: export_queue
    fetcher = _MarkerFetcher(fetched_text, fetched_title)
    app.dependency_overrides[get_job_posting_fetcher] = lambda: fetcher

    ids: dict[str, str] = {}

    def lines_naming(value: str, since: int) -> int:
        return sum(value in r.getMessage() for r in caplog.records[since:])

    with caplog.at_level(logging.DEBUG):
        # --- register, saved CV, postings ---------------------------------------------------------
        token, seeded_id = await seed_user_and_sign_in(client, settings, email=email)
        headers = bearer(token)
        ids["user_id"] = str(seeded_id)
        uploaded = await client.post(
            ME_BASE_CVS, files={"file": (filename, cv_text.encode(), "text/plain")}, headers=headers
        )
        assert uploaded.status_code == 201, uploaded.text
        ids["cv"] = str(uploaded.json()["id"])
        renamed = await client.patch(
            f"{ME_BASE_CVS}/{ids['cv']}", json={"label": label}, headers=headers
        )
        assert renamed.status_code == 200, renamed.text
        pasted = await client.post(
            ME_POSTINGS, json={"source": "pasted", "text": pasted_text}, headers=headers
        )
        assert pasted.status_code == 201, pasted.text
        ids["pasted"] = str(pasted.json()["id"])
        fetched = await client.post(
            ME_POSTINGS, json={"source": "fetched", "url": fetched_url}, headers=headers
        )
        assert fetched.status_code == 201, fetched.text
        assert fetcher.calls == 1
        ids["fetched"] = str(fetched.json()["id"])

        # --- run one (fake LLM, marker documents) -------------------------------------------------
        run_id, call = await _work_a_run(
            client,
            session,
            settings,
            headers,
            {"base_cv_id": ids["cv"], "job_posting_id": ids["pasted"]},
            _draft(run_cv, run_letter),
        )
        cv_sent, posting_sent = call
        for forbidden in (email, ids["user_id"], title_one, title_two, label, filename):
            assert forbidden not in cv_sent.value
            assert forbidden not in posting_sent.value
        assert len(tailoring_queue.enqueued) == 1

        # --- track with a marker title -------------------------------------------------------------
        mark = len(caplog.records)
        tracked = await client.post(
            ME_TRACKED,
            json={"tailoring_run_id": run_id, "title": title_one},
            headers=headers,
        )
        assert tracked.status_code == 201, tracked.text
        card_id = str(tracked.json()["id"])
        assert tracked.json()["title"] == title_one
        assert lines_naming(card_id, mark) >= 1, "tracking: capture not live"
        assert await assert_no_card_crosses_owners(session, [seeded_id]) == 1

        # --- move x3 --------------------------------------------------------------------------------
        for version, stage in enumerate(("applied", "interviewing", "offer"), start=1):
            mark = len(caplog.records)
            moved = await client.put(
                f"{ME_TRACKED}/{card_id}/stage",
                json={"stage": stage, "version": version},
                headers=headers,
            )
            assert moved.status_code == 200, moved.text
            assert moved.json()["stage"] == stage
            assert moved.json()["version"] == version + 1
            assert lines_naming(card_id, mark) >= 1, f"move to {stage}: capture not live"
        assert await assert_no_card_crosses_owners(session, [seeded_id]) == 1

        # --- a refused (422) title, then a retitle --------------------------------------------------
        refused = await client.put(
            f"{ME_TRACKED}/{card_id}/title",
            json={"title": refused_title + "x" * 120, "version": 4},
            headers=headers,
        )
        assert refused.status_code == 422, refused.text
        retitled = await client.put(
            f"{ME_TRACKED}/{card_id}/title",
            json={"title": title_two, "version": 4},
            headers=headers,
        )
        assert retitled.status_code == 200, retitled.text
        assert retitled.json()["title"] == title_two

        # --- board ----------------------------------------------------------------------------------
        board = await client.get(ME_BOARD, headers=headers)
        assert board.status_code == 200, board.text
        assert [item["id"] for item in board.json()["items"]] == [card_id]
        assert board.json()["items"][0]["title"] == title_two

        # --- untrack, track again -------------------------------------------------------------------
        mark = len(caplog.records)
        untracked = await client.delete(f"{ME_TRACKED}/{card_id}", headers=headers)
        assert untracked.status_code == 204, untracked.text
        assert lines_naming(card_id, mark) >= 1, "untrack: capture not live"
        again = await client.post(ME_TRACKED, json={"tailoring_run_id": run_id}, headers=headers)
        assert again.status_code == 201, again.text
        again_id = str(again.json()["id"])
        assert again_id != card_id
        assert await assert_no_card_crosses_owners(session, [seeded_id]) == 1

        # --- delete the saved CV, board -------------------------------------------------------------
        removed_cv = await client.delete(f"{ME_BASE_CVS}/{ids['cv']}", headers=headers)
        assert removed_cv.status_code == 204, removed_cv.text
        board = await client.get(ME_BOARD, headers=headers)
        assert board.status_code == 200, board.text
        (item,) = board.json()["items"]
        assert item["id"] == again_id
        assert item["base_cv"] is None, "the deleted CV's label and filename must not be lent"

        # --- delete the history entry: the card goes with it -----------------------------------------
        mark = len(caplog.records)
        erased_entry = await client.delete(f"{ME_RUNS}/{run_id}", headers=headers)
        assert erased_entry.status_code == 204, erased_entry.text
        assert lines_naming(run_id, mark) >= 1, "entry deletion: capture not live"
        board = await client.get(ME_BOARD, headers=headers)
        assert board.status_code == 200, board.text
        assert board.json() == {"items": []}

        # --- a second saved CV, a second run on the fetched posting, tracked ------------------------
        second_cv_upload = await client.post(
            ME_BASE_CVS,
            files={"file": (second_filename, cv_text.encode(), "text/plain")},
            headers=headers,
        )
        assert second_cv_upload.status_code == 201, second_cv_upload.text
        second_run_id, second_call = await _work_a_run(
            client,
            session,
            settings,
            headers,
            {
                "base_cv_id": str(second_cv_upload.json()["id"]),
                "job_posting_id": ids["fetched"],
            },
            _draft(second_cv, second_letter),
        )
        assert fetched_title not in second_call[0].value
        assert fetched_title not in second_call[1].value
        assert fetched_url not in second_call[1].value
        mark = len(caplog.records)
        second_tracked = await client.post(
            ME_TRACKED,
            json={"tailoring_run_id": second_run_id, "title": title_three, "stage": "applied"},
            headers=headers,
        )
        assert second_tracked.status_code == 201, second_tracked.text
        second_card = str(second_tracked.json()["id"])
        assert lines_naming(second_card, mark) >= 1, "second tracking: capture not live"
        assert await assert_no_card_crosses_owners(session, [seeded_id]) == 1

        # --- erase the account ------------------------------------------------------------------------
        mark = len(caplog.records)
        deleted = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": A_PASSWORD},
            headers={**headers, "Origin": settings.public_base_url},
        )
        assert deleted.status_code == 204, deleted.text
        assert lines_naming(ids["user_id"], mark) >= 1, "erasure: capture not live"
        assert await card_count_on_session(session, seeded_id) == 0

    # --- AC-41's runtime half: tracking enqueued nothing and called no LLM ------------------------
    assert len(tailoring_queue.enqueued) == 2, "exactly the two runs were enqueued — never a track"
    assert export_queue.enqueued == []

    # --- the tee holds what the flow must have produced (so "no marker in events" is not vacuous) --
    assert [type(e) for e in publisher.events if isinstance(e, ApplicationTracked)] == [
        ApplicationTracked
    ] * 3
    assert len([e for e in publisher.events if isinstance(e, ApplicationStageChanged)]) == 3
    assert len([e for e in publisher.events if isinstance(e, ApplicationUntracked)]) == 1

    # --- Sentry: the channel is live ------------------------------------------------------------------
    sentry_sdk.capture_message("QA42-sentry-probe")
    sentry_sdk.flush()
    sentry_text = "\n".join(sentry_envelopes)
    assert "QA42-sentry-probe" in sentry_text, "the Sentry capture is not live"

    # --- The claim -------------------------------------------------------------------------------------
    markers = {
        "the email": email,
        "the CV filename": filename,
        "the second CV filename": second_filename,
        "the CV label": label,
        "the CV text": cv_text.split(" ")[0],
        "the pasted posting text": pasted_text.split(" ")[0],
        "the fetched title": fetched_title,
        "the fetched URL": fetched_url.rsplit("/", 1)[1],
        "the tailored CV": run_cv,
        "the cover letter": run_letter,
        "the second tailored CV": second_cv,
        "the second cover letter": second_letter,
        "the first card title": title_one,
        "the retitle": title_two,
        "the refused (422) title": refused_title,
        "the second run's card title": title_three,
    }
    event_text = _event_text(publisher.events)
    assert publisher.events, "the API published no domain event — the tee is not wired"
    redis = create_redis(settings.redis_url)
    try:
        redis_keys = "\n".join(
            [key.decode() if isinstance(key, bytes) else key async for key in redis.scan_iter("*")]
        )
    finally:
        await redis.aclose()
    assert redis_keys, "Redis holds no key — the limiter was not exercised, the scan is vacuous"
    for description, marker in markers.items():
        assert marker not in caplog.text, f"{description} ({marker}) reached a log record"
        assert marker not in event_text, f"{description} ({marker}) reached a domain event"
        assert marker not in sentry_text, f"{description} ({marker}) reached a Sentry envelope"
        assert marker not in redis_keys, f"{description} ({marker}) reached a Redis key"


async def card_count_on_session(session: AsyncSession, user_id: UUID) -> int:
    """The user's cards as the test's own session sees them (the shared-session `app` fixture: every
    request ran on it, so an erasure's cascade is visible here)."""
    result = await session.execute(
        text("SELECT count(*) FROM tracking_application WHERE user_id = :u"), {"u": user_id}
    )
    return int(result.scalar_one())


# --- erase-account over a marker title ---------------------------------------------------------------


async def test_ac42_erase_account_over_a_marker_title_logs_the_count_and_never_the_title(
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Green on arrival: `EraseAccount` landed at T17. Pins that the card's free text never reaches
    a log record on the operator path — and that the capture is live (the user's id, the count)."""
    assert_test_database(settings)
    configure_logging(settings)
    title = _marker("erase-title")
    email = f"{_marker('erase-email').lower()}@example.com"
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        user_id = await seed_user(seeding, clock, email)
        account = Account(token="", owner=UserOwner(user_id))
        await seed_card(
            seeding, settings, account, at=clock.now() - timedelta(minutes=10), title=title
        )
    try:
        assert await card_count_on(engine, user_id.value) == 1

        with caplog.at_level(logging.DEBUG):
            exit_code = await erase_account_command.erase_account(
                settings, user_id=UserId(user_id.value), dry_run=False
            )

        assert exit_code == erase_account_command.EXIT_OK, caplog.text
        assert str(user_id.value) in caplog.text, "erase-account: capture not live"
        assert "retention.account_erased" in caplog.text
        assert title not in caplog.text
        assert email not in caplog.text
        assert title not in capsys.readouterr().out
        assert await card_count_on(engine, user_id.value) == 0
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id.value}
            )
