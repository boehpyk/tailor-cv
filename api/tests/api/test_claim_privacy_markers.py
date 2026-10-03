"""AC-46 / AC-47 — the planted-marker test for the guest-work claim, and "the LLM sees nothing new"
(slice 2.4, T21 RED).

**Hard from the start** (2.2's `/verify` lesson, in CLAUDE.md): every step asserts its own exact
status in the spec's order; the fake `LlmPort`'s **call count is asserted before its arguments**;
nothing is gated on a previous step having worked, so a claim that starts answering 409 turns the
test red instead of skipping its own flow. Against the T19 skeleton the guest half runs for real (every
guest route is landed code) and the test goes red at the claim itself — `POST /api/me/guest-work/claim`,
`assert 500 == 200` — which is the honest red: every later marker is planted only once T22 makes the
route real. **No soft gate is used, so there is nothing for T22b to remove.**

**What "no marker" covers** (AC-46), in the first test: every captured log record (stdlib and structlog,
every logger, `caplog` at DEBUG on the root), which includes `LoggingEventPublisher`'s rendering of every
domain event the API *and* the worker publish; every domain event the API publishes, field by field (a
tee publisher on `get_event_publisher`); every Sentry envelope (initialised through the real
`configure_sentry` with a capturing transport; a probe proves the channel is live); every Redis key.
Markers sit in: a guest CV's filename and text, a pasted posting, a fetched posting's title and URL, both
tailored documents, an edit, and the account's email. The drive: guest flow -> register -> claim ->
history -> re-open -> re-export -> a second run on the claimed CV -> erase the account.

**The second test** takes the claimed work through a purge and an orphan sweep and `erase-account`
over **committed** rows, through their real entry points (2.2's and 2.3's harness): those composition
roots open their own engine and cannot see the rolled-back test transaction, so the claim is made over
`build_concurrent_app` (a real session per request, real commits). The purge has no clock seam, so
"+30 days" is not staged by moving a clock: the claimed files are aged 60 h on disk (past the sweep's
window and grace), the claimed session is already gone, and the proof that they survive is that a real
purge and a real sweep leave them standing while a planted expired guest and a planted orphan go.

**Positive controls**, because every assertion below is an absence: the claim's own line
(`identity.guest_work_claimed`) is captured and read field by field; each flow names its ids in at
least one captured line; the Sentry probe is delivered; the tee saw events.

**AC-47's two clauses that are not in this file:** `git diff main --stat -- api/src/tailorcraft/
infrastructure/llm` empty is a `/verify` gate (no `.git` in the test container); the signature pin of
`LlmPort.tailor` is repeated here because AC-47 names it.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sentry_sdk
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.export.value_objects import ExportJobStatus
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.value_objects import ExtractedText
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.ports import LlmPort
from tailorcraft.domain.tailoring.value_objects import TailoredDraft, TailoringRunId
from tailorcraft.infrastructure import observability
from tailorcraft.infrastructure.api.deps import (
    get_event_publisher,
    get_export_queue,
    get_job_posting_fetcher,
    get_tailoring_queue,
)
from tailorcraft.infrastructure.api.guest_session import (
    COOKIE_NAME,
    hash_guest_token,
    mint_guest_token,
)
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention import erase_account_command, purge_command
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container as tailoring_container
from tests.api.me_support import (
    A_PASSWORD,
    DELETE_ACCOUNT_URL,
    ME_RUNS,
    assert_test_database,
    bearer,
    build_concurrent_app,
    new_client,
    seed_user_and_sign_in,
)
from tests.api.test_export import _run_export_worker
from tests.api.test_history_privacy_markers import (
    _assert_llm_saw_only_the_two_texts,
    _CapturingTransport,
    _committed_history,
    _draft,
    _event_text,
    _MarkerFetcher,
    _TeePublisher,
)
from tests.integration.fakes import (
    FakeDocumentRenderer,
    FakeExportQueue,
    FakeLlm,
    FakeTailoringQueue,
    InMemoryFileStore,
)

CLAIM_URL = "/api/me/guest-work/claim"
GUEST_BASE_CVS = "/api/base-cvs"
GUEST_POSTINGS = "/api/job-postings"
GUEST_RUNS = "/api/tailoring-runs"
CLAIM_EVENT: Final = "identity.guest_work_claimed"
_PREFIX: Final = "QA46MARKER"


def _marker(label: str) -> str:
    return f"{_PREFIX}-{label}-{uuid4().hex}"


# --- Capture channels -----------------------------------------------------------------------------


@pytest.fixture
def sentry_envelopes(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Sentry initialised through the real `configure_sentry` (so the PII settings are production's),
    with the transport swapped for a capturing one. Torn down to "no client" afterwards."""
    transport = _CapturingTransport()
    real_init = sentry_sdk.init

    def _init(*args: Any, **kwargs: Any) -> Any:  # Any: sentry_sdk.init's own signature
        return real_init(*args, transport=transport, **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", _init)
    observability.configure_sentry(
        settings.model_copy(update={"sentry_dsn": "https://public@sentry.example.invalid/1"})
    )
    try:
        yield transport.envelopes
    finally:
        real_init()  # no DSN: a disabled client for every later test


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every step can touch a limiter; the rollback never reaches Redis."""


@pytest_asyncio.fixture
async def committed_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)() as s:
        yield s


def _claim_lines(records: list[logging.LogRecord]) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in records if CLAIM_EVENT in r.getMessage()]


def _assert_the_claim_line_is_counts_and_a_user_id_only(
    line: dict[str, Any],
    *,
    user_id: str,
    counts: dict[str, int],
    never: dict[str, str],
) -> None:
    """AC-46: `user_id` and the five counts, and **no** session id, token, hash or key — by value
    anywhere in the line, and by field name."""
    assert line["user_id"] == user_id
    for name, expected in counts.items():
        assert line[name] == expected, (name, line)
    rendered = json.dumps(line)
    for description, value in never.items():
        assert value not in rendered, f"{description} is in the claim's log line: {rendered}"
    for field in line:
        lowered = field.lower()
        assert not any(part in lowered for part in ("session", "token", "hash", "key", "path")), (
            f"the claim's log line carries a field named {field!r}: {rendered}"
        )


async def _run_tailoring_worker(
    session: AsyncSession, settings: Settings, run_id: str, draft: TailoredDraft
) -> tuple[FakeLlm, ExecuteTailoringRunOutcome]:
    fake_llm = FakeLlm(draft)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tailoring_container, "GeminiLlm", lambda settings: fake_llm)
        execute = tailoring_container._build_use_case(settings, session)
        outcome = await execute(
            ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(UUID(run_id)))
        )
    return fake_llm, outcome


# --- AC-47 (b): the port's signature --------------------------------------------------------------


def test_ac47_the_llm_port_signature_is_pinned() -> None:
    """Green on arrival — `LlmPort` is untouched by 2.4, and this pins that it stays so."""
    signature = inspect.signature(LlmPort.tailor)

    assert list(signature.parameters) == ["self", "cv", "posting"]
    assert signature.parameters["cv"].annotation in ("ExtractedText", ExtractedText)
    assert signature.parameters["posting"].annotation in ("JobPostingText", JobPostingText)


# --- AC-46 + AC-47: guest -> register -> claim -> history -> ... -> erase -----------------------


async def test_ac46_no_marker_leaks_across_a_claim_and_ac47_the_llm_sees_nothing_new(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
    sentry_envelopes: list[str],
) -> None:
    email = f"{_marker('email').lower()}@example.com"
    filename = _marker("filename") + ".txt"
    cv_text = " ".join([_marker("cv-text")] + ["experienced platform engineer."] * 30)
    pasted_text = _marker("pasted-posting") + " " + "we are hiring a platform engineer. " * 10
    fetched_title = _marker("fetched-title")
    fetched_url = f"https://jobs.example.com/{_marker('fetched-url')}"
    fetched_text = "Senior platform engineer, remote, fully async team. " * 6
    run_cv, run_letter = _marker("tailored-cv"), _marker("cover-letter")
    second_cv, second_letter = _marker("second-cv"), _marker("second-letter")
    edit_text = _marker("edit") + " " + "revised curriculum vitae line. " * 20
    export_bytes = f"%PDF-1.7 {_marker('export-bytes')}".encode()

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
        # --- the guest: upload, paste, fetch, run, edit --------------------------------------------
        uploaded = await client.post(
            GUEST_BASE_CVS, files={"file": (filename, cv_text.encode(), "text/plain")}
        )
        assert uploaded.status_code == 201, uploaded.text
        ids["base_cv_id"] = str(uploaded.json()["id"])
        guest_token = client.cookies.get(COOKIE_NAME)
        assert guest_token, "the upload left no tc_guest cookie"
        session_id = (
            await session.execute(
                text("SELECT id FROM identity_guest_session WHERE token_hash = :h"),
                {"h": hash_guest_token(guest_token)},
            )
        ).scalar_one()
        file_key = (
            await session.execute(
                text("SELECT file_key FROM intake_base_cv WHERE id = :id"),
                {"id": UUID(ids["base_cv_id"])},
            )
        ).scalar_one()

        pasted = await client.post(GUEST_POSTINGS, json={"source": "pasted", "text": pasted_text})
        assert pasted.status_code == 201, pasted.text
        ids["pasted_posting_id"] = str(pasted.json()["id"])
        fetched = await client.post(GUEST_POSTINGS, json={"source": "fetched", "url": fetched_url})
        assert fetched.status_code == 201, fetched.text
        assert fetcher.calls == 1
        ids["fetched_posting_id"] = str(fetched.json()["id"])

        requested = await client.post(
            GUEST_RUNS,
            json={"base_cv_id": ids["base_cv_id"], "job_posting_id": ids["pasted_posting_id"]},
        )
        assert requested.status_code == 202, requested.text
        ids["guest_run_id"] = str(requested.json()["id"])
        guest_llm, outcome = await _run_tailoring_worker(
            session, settings, ids["guest_run_id"], _draft(run_cv, run_letter)
        )
        assert len(guest_llm.calls) == 1, "the LLM is called exactly once per run"
        assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED

        guest_run = await client.get(f"{GUEST_RUNS}/{ids['guest_run_id']}")
        assert guest_run.status_code == 200, guest_run.text
        edited = await client.put(
            f"{GUEST_RUNS}/{ids['guest_run_id']}/documents/cv",
            json={"content": edit_text, "expected_version": guest_run.json()["version"]},
        )
        assert edited.status_code == 200, edited.text

        # --- register ---------------------------------------------------------------------------
        token, seeded_id = await seed_user_and_sign_in(client, settings, email=email)
        headers = bearer(token)
        ids["user_id"] = str(seeded_id)

        # --- the claim: the fake's count asserted BEFORE anything about its arguments --------------
        calls_before_the_claim = len(guest_llm.calls)
        mark = len(caplog.records)
        claimed = await client.post(CLAIM_URL, headers=headers)
        assert claimed.status_code == 200, claimed.text
        assert claimed.json() == {
            "base_cvs": 1,
            "job_postings": 2,
            "tailoring_runs": 1,
            "export_jobs": 0,
            "working_copies_dropped": 0,
        }
        assert len(guest_llm.calls) == calls_before_the_claim == 1, (
            "the claim called the LLM: the paid call must never be repeated or triggered by it"
        )
        (claim_line,) = _claim_lines(caplog.records[mark:])
        _assert_the_claim_line_is_counts_and_a_user_id_only(
            claim_line,
            user_id=ids["user_id"],
            counts={
                "base_cvs": 1,
                "job_postings": 2,
                "tailoring_runs": 1,
                "export_jobs": 0,
                "working_copies_dropped": 0,
            },
            never={
                "the guest session id": str(session_id),
                "the guest token": guest_token,
                "the token hash": hash_guest_token(guest_token),
                "the file key": file_key,
            },
        )

        # --- history, re-open, re-export ---------------------------------------------------------
        history = await client.get(ME_RUNS, headers=headers)
        assert history.status_code == 200, history.text
        assert [item["id"] for item in history.json()["items"]] == [ids["guest_run_id"]]
        reopened = await client.get(f"{ME_RUNS}/{ids['guest_run_id']}", headers=headers)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["expires_at"] is None
        inline = await client.get(
            f"{ME_RUNS}/{ids['guest_run_id']}/documents/cv/download?format=md", headers=headers
        )
        assert inline.status_code == 200, inline.text
        requested_export = await client.post(
            f"{ME_RUNS}/{ids['guest_run_id']}/exports",
            json={"document": "cv", "format": "pdf"},
            headers=headers,
        )
        assert requested_export.status_code == 202, requested_export.text
        job_id = str(requested_export.json()["id"])
        await _run_export_worker(
            session,
            settings,
            job_id,
            renderer=FakeDocumentRenderer(export_bytes),
            files=InMemoryFileStore(),
        )
        polled = await client.get(f"/api/me/export-jobs/{job_id}", headers=headers)
        assert polled.status_code == 200, polled.text
        assert polled.json()["status"] == ExportJobStatus.READY.value

        # --- a second run, on the CLAIMED CV and posting: the LLM sees only the two texts ----------
        forbidden_to_llm = {
            "the email": email,
            "the user id": ids["user_id"],
            "the CV id": ids["base_cv_id"],
            "the pasted posting id": ids["pasted_posting_id"],
            "the guest session id": str(session_id),
            "the guest token": guest_token,
            "the CV filename": filename,
            "the posting URL": fetched_url,
            "the posting title": fetched_title,
        }
        second = await client.post(
            ME_RUNS,
            json={"base_cv_id": ids["base_cv_id"], "job_posting_id": ids["fetched_posting_id"]},
            headers=headers,
        )
        assert second.status_code == 202, second.text
        second_run_id = str(second.json()["id"])
        second_llm, second_outcome = await _run_tailoring_worker(
            session, settings, second_run_id, _draft(second_cv, second_letter)
        )
        assert len(second_llm.calls) == 1, "exactly one LLM call per run"
        assert second_outcome is ExecuteTailoringRunOutcome.SUCCEEDED
        assert len(guest_llm.calls) == 1, "the first run's call count moved after the claim"
        _assert_llm_saw_only_the_two_texts(
            second_llm.calls[0],
            cv_text=cv_text,
            posting_text=fetched_text,
            forbidden={**forbidden_to_llm, "the run id": second_run_id},
        )
        _assert_llm_saw_only_the_two_texts(
            guest_llm.calls[0],
            cv_text=cv_text,
            posting_text=pasted_text,
            forbidden={**forbidden_to_llm, "the run id": ids["guest_run_id"]},
        )
        assert lines_naming(second_run_id, mark) >= 1, "run: capture not live"

        # --- erase the account -----------------------------------------------------------------------
        mark = len(caplog.records)
        deleted = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": A_PASSWORD},
            headers={**headers, "Origin": settings.public_base_url},
        )
        assert deleted.status_code == 204, deleted.text
        assert lines_naming(ids["user_id"], mark) >= 1, "erasure: capture not live"

    # --- Sentry: the channel is live ---------------------------------------------------------------
    sentry_sdk.capture_message("QA46-sentry-probe")
    sentry_sdk.flush()
    sentry_text = "\n".join(sentry_envelopes)
    assert "QA46-sentry-probe" in sentry_text, "the Sentry capture is not live"

    # --- The claim of this test: no marker anywhere it must not be ---------------------------------
    markers = {
        "the email": email,
        "the CV filename": filename,
        "the CV text": cv_text.split(" ")[0],
        "the pasted posting text": pasted_text.split(" ")[0],
        "the fetched title": fetched_title,
        "the fetched URL": fetched_url.rsplit("/", 1)[1],
        "the tailored CV": run_cv,
        "the cover letter": run_letter,
        "the second tailored CV": second_cv,
        "the second cover letter": second_letter,
        "the edit": edit_text.split(" ")[0],
        "the export bytes": export_bytes.decode().split(" ")[1],
        "the guest token": guest_token,
        "the token hash": hash_guest_token(guest_token),
        "the file key": file_key,
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
    for description, marker in markers.items():
        assert marker not in caplog.text, f"{description} ({marker}) reached a log record"
        assert marker not in event_text, f"{description} ({marker}) reached a domain event"
        assert marker not in sentry_text, f"{description} ({marker}) reached a Sentry envelope"
        assert marker not in redis_keys, f"{description} ({marker}) reached a Redis key"


# --- AC-46: the claimed work through a purge, an orphan sweep and erase-account -------------------


async def test_ac46_a_claim_then_a_purge_a_sweep_and_an_erasure_never_log_a_marker(
    settings: Settings,
    engine: AsyncEngine,
    committed_session: AsyncSession,
    password_hasher: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live guest session holding an entry (CV, posting, run, a ready PDF) is claimed over real
    commits. A planted EXPIRED guest holds one too, and a true orphan sits on the volume. A real purge
    takes the expired guest, a real sweep takes the orphan, **and both leave the claimed export**
    (aged 60 h on disk: unreferenced, the sweep would take it); `erase-account` then takes it. No
    marker, key, joined path, token or session id in any record; the claim's line is read field by
    field; each run's completion line is present."""
    assert_test_database(settings)
    local_settings = settings.model_copy(update={"upload_dir": tmp_path})
    files = LocalFileStore(tmp_path)
    now = SystemClock().now()
    claimed_marker, expired_marker, orphan_marker = (
        _marker("CLAIMED"),
        _marker("EXPIRED-GUEST"),
        _marker("ORPHAN"),
    )
    email = f"{_marker('claimant-email').lower()}@example.com"
    minted = mint_guest_token()

    live_id = GuestSessionId(uuid4())
    expired_id = GuestSessionId(uuid4())
    await committed_session.execute(
        guest_session_table.insert().values(
            id=live_id,
            token_hash=minted.token_hash,
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
    await committed_session.execute(
        guest_session_table.insert().values(
            id=expired_id,
            token_hash=uuid4().hex * 2,
            created_at=now - timedelta(hours=25),
            expires_at=now - timedelta(hours=1),
        )
    )
    await committed_session.commit()
    claimed_ref = await _committed_history(
        committed_session, files, GuestOwner(live_id), now, claimed_marker
    )
    expired_ref = await _committed_history(
        committed_session, files, GuestOwner(expired_id), now, expired_marker
    )
    orphan_ref = FileRef(key=f"{uuid4().hex[:2]}/{uuid4().hex[2:4]}/{uuid4()}.pdf")
    await files.put(orphan_ref, orphan_marker.encode())
    old = (now - timedelta(hours=60)).timestamp()
    for ref in (claimed_ref, orphan_ref):
        os.utime(tmp_path / ref.key, (old, old))

    user_id: UserId | None = None
    api = build_concurrent_app(local_settings, engine, password_hasher)
    try:
        async with new_client(api) as http:
            with caplog.at_level(logging.DEBUG):
                claim_token, seeded_id = await seed_user_and_sign_in(
                    http, local_settings, email=email
                )
                user_id = UserId(seeded_id)
                http.cookies.set(COOKIE_NAME, minted.token)

                mark = len(caplog.records)
                claimed = await http.post(CLAIM_URL, headers=bearer(claim_token))
                assert claimed.status_code == 200, claimed.text
                assert claimed.json() == {
                    "base_cvs": 1,
                    "job_postings": 1,
                    "tailoring_runs": 1,
                    "export_jobs": 1,
                    "working_copies_dropped": 0,
                }
                (claim_line,) = _claim_lines(caplog.records[mark:])
                _assert_the_claim_line_is_counts_and_a_user_id_only(
                    claim_line,
                    user_id=str(user_id.value),
                    counts=claimed.json(),
                    never={
                        "the guest session id": str(live_id.value),
                        "the guest token": minted.token,
                        "the token hash": minted.token_hash,
                        "the claimed file key": claimed_ref.key,
                        "the claimed joined path": str(tmp_path / claimed_ref.key),
                    },
                )

                purge_exit = await purge_command._purge_guests(
                    local_settings, dry_run=False, limit=None
                )
                sweep_exit = await purge_command._reclaim_orphans(
                    local_settings, dry_run=False, limit=None, grace_hours=24
                )
                assert purge_exit == purge_command.EXIT_OK, caplog.text
                assert sweep_exit == purge_command.EXIT_OK, caplog.text
                assert "retention.purge_completed" in caplog.text
                assert "retention.orphan_scan_completed" in caplog.text
                assert not (tmp_path / expired_ref.key).exists(), "the expired guest's file stayed"
                assert not (tmp_path / orphan_ref.key).exists(), "the orphan stayed"
                assert (tmp_path / claimed_ref.key).exists(), (
                    "a purge or a sweep took a CLAIMED file"
                )

                erased_exit = await erase_account_command.erase_account(
                    local_settings, user_id=user_id, dry_run=False
                )
                assert erased_exit == erase_account_command.EXIT_OK, caplog.text
                assert str(user_id.value) in caplog.text, "erase-account: capture not live"
                assert not (tmp_path / claimed_ref.key).exists(), "erasure left the claimed file"

        forbidden = {
            "the claimant's email": email,
            "the claimed posting text / bytes": claimed_marker,
            "the expired guest's posting text / bytes": expired_marker,
            "the orphan's bytes": orphan_marker,
            "the guest token": minted.token,
            "the token hash": minted.token_hash,
            "the claimed session id": str(live_id.value),
            **{
                f"a storage key ({ref.key})": ref.key
                for ref in (claimed_ref, expired_ref, orphan_ref)
            },
            **{
                f"a joined path ({ref.key})": str(tmp_path / ref.key)
                for ref in (claimed_ref, expired_ref, orphan_ref)
            },
        }
        for description, marker in forbidden.items():
            assert marker not in caplog.text, f"{description} reached a log record"
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                guest_session_table.delete().where(
                    guest_session_table.c.id.in_([live_id, expired_id])
                )
            )
            if user_id is not None:
                await conn.execute(user_table.delete().where(user_table.c.id == user_id))
