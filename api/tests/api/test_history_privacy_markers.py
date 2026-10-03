"""AC-53 / AC-54 — the planted-marker test for tailoring history, and "the LLM sees nothing new"
(slice 2.3, T22 RED).

**Hard from the start.** Every step asserts its own exact status, in the spec's order; the fake
LLM's **call count is asserted before its arguments**; nothing is gated on a previous step having
worked (2.2's `/verify` MAJOR: a soft test that can skip its own flow passes green without its
claim). Against the T20 skeleton the HTTP flow is therefore red at its first account-route step —
`POST /api/me/job-postings`, `assert 500 == 201` — which is the honest red: every later marker is
planted only once T23 makes each step real. **No soft gate is used, so there is nothing for the
post-T23 commit to remove.**

**What "no marker" covers** (AC-53): every captured log record (stdlib and structlog, every logger —
`caplog` at DEBUG on the root), which includes `LoggingEventPublisher`'s rendering of every domain
event the API *and* the worker publish; every domain event the API publishes, field by field (a tee
publisher on `get_event_publisher`); every Sentry envelope — Sentry is initialised through the real
`configure_sentry` with a capturing transport, a probe proves the channel is live, and a final
message carries the flow's breadcrumbs; every Redis key. **Positive controls**: one line per flow
names the `tailoring_run_id` / `user_id` it is about, so the capture is proven live rather than
assumed.

**The second test** drives `erase-account` on a second user, then a purge and an orphan sweep, over
2.3's shapes, through their real entry points against committed rows (2.2's harness, reproduced) —
those composition roots open their own engine and cannot see the rolled-back test transaction.

**AC-54's third clause** (`git diff main --stat -- api/src/tailorcraft/infrastructure/llm` empty)
cannot run inside the test container — `/app` is `./api`, with no `.git` — and stays a `/verify`
gate, as 2.1's AC-52 and 2.2's AC-58 were.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import os
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sentry_sdk
from fastapi import FastAPI
from httpx import AsyncClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobStatus
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.intake.value_objects import ExtractedText
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingText,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.ports import LlmPort
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoredDraft,
    TailoringRunId,
)
from tailorcraft.infrastructure import observability
from tailorcraft.infrastructure.api.deps import (
    get_event_publisher,
    get_export_queue,
    get_job_posting_fetcher,
    get_tailoring_queue,
)
from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
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
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention import erase_account_command, purge_command
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container as tailoring_container
from tests.api.me_support import (
    A_PASSWORD,
    DELETE_ACCOUNT_URL,
    ME_BASE_CVS,
    ME_POSTINGS,
    ME_RUNS,
    assert_test_database,
    bearer,
    new_client,
    seed_user_and_sign_in,
)
from tests.api.test_export import _run_export_worker
from tests.integration.fakes import (
    FakeDocumentRenderer,
    FakeExportQueue,
    FakeLlm,
    FakeTailoringQueue,
    InMemoryFileStore,
)
from tests.integration.owners import extracted_cv, pasted_posting, ready_export, succeeded_run

_PREFIX: Final = "QA53MARKER"


def _marker(label: str) -> str:
    return f"{_PREFIX}-{label}-{uuid4().hex}"


# --- Capture channels -----------------------------------------------------------------------------


class _CapturingTransport(Transport):
    """Keeps every envelope Sentry would have sent, serialized, for the marker scan."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:  # Any: sentry's own typing
        super().__init__(options)
        self.envelopes: list[str] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope.serialize().decode("utf-8", errors="replace"))

    def flush(self, timeout: float, callback: Any | None = None) -> None:  # Any: sentry's typing
        return None

    def kill(self) -> None:
        return None


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


class _TeePublisher:
    """Records every event the API publishes and still logs it (the positive controls read the
    `domain_event` lines)."""

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []
        self._inner = LoggingEventPublisher()

    async def publish(self, *events: DomainEvent) -> None:
        self.events.extend(events)
        await self._inner.publish(*events)


def _event_text(events: Sequence[DomainEvent]) -> str:
    """Every field of every event, rendered — `asdict` recurses into value objects."""
    return "\n".join(repr(dataclasses.asdict(event)) for event in events)


class _MarkerFetcher:
    def __init__(self, text: str, title: str) -> None:
        self._posting = FetchedPosting(text=JobPostingText(text), title=PostingTitle(title))
        self.calls = 0

    async def fetch(self, url: SourceUrl) -> FetchedPosting:
        self.calls += 1
        return self._posting


def _draft(cv_marker: str, letter_marker: str) -> TailoredDraft:
    return TailoredDraft(
        documents=TailoredDocuments(
            cv=TailoredCv(f"{cv_marker} " + "tailored curriculum vitae line. " * 20),
            cover_letter=CoverLetter(f"{letter_marker} " + "dear hiring team, " * 20),
        ),
        metrics=LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1,
        ),
    )


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every step can touch a limiter; the rollback never reaches Redis."""


async def _work_a_run(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    headers: dict[str, str],
    body: dict[str, str],
    draft: TailoredDraft,
) -> tuple[str, tuple[ExtractedText, JobPostingText]]:
    """Request a run over the account route and execute it through the worker's own composition
    root with a fake LLM. Returns the run id and the fake's **one** call — its count asserted first."""
    requested = await client.post(ME_RUNS, json=body, headers=headers)
    assert requested.status_code == 202, requested.text
    run_id = str(requested.json()["id"])

    fake_llm = FakeLlm(draft)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tailoring_container, "GeminiLlm", lambda settings: fake_llm)
        execute = tailoring_container._build_use_case(settings, session)
        outcome = await execute(
            ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(UUID(run_id)))
        )

    assert len(fake_llm.calls) == 1, (
        f"the LLM is called exactly once per run, not {len(fake_llm.calls)}"
    )
    assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED
    return run_id, fake_llm.calls[0]


def _assert_llm_saw_only_the_two_texts(
    call: tuple[ExtractedText, JobPostingText],
    *,
    cv_text: str,
    posting_text: str,
    forbidden: dict[str, str],
) -> None:
    """AC-54: the arguments are the CV text and the posting text — nothing else."""
    cv_sent, posting_sent = call
    assert cv_sent == ExtractedText(cv_text)
    assert posting_sent == JobPostingText(posting_text)
    for description, value in forbidden.items():
        assert value not in cv_sent.value, f"{description} reached the LLM in the CV argument"
        assert value not in posting_sent.value, f"{description} reached the LLM in the posting"


# --- AC-54 (b): the port's signature -------------------------------------------------------------


def test_ac54_the_llm_port_signature_is_pinned() -> None:
    """Green on arrival — `LlmPort` is untouched by 2.3, and this pins that it stays so."""
    signature = inspect.signature(LlmPort.tailor)

    assert list(signature.parameters) == ["self", "cv", "posting"]
    assert signature.parameters["cv"].annotation in ("ExtractedText", ExtractedText)
    assert signature.parameters["posting"].annotation in ("JobPostingText", JobPostingText)


# --- AC-53 + AC-54: the account flow ---------------------------------------------------------------


async def test_ac53_no_marker_leaks_across_the_history_flow_and_the_llm_sees_only_the_texts(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
    sentry_envelopes: list[str],
) -> None:
    """register → saved CV (marker filename, text, label) → account posting (paste, then fetch with
    a marker title and URL) → run (fake LLM, marker documents) → edit → history → PDF export (fake
    renderer, marker bytes) → re-open → delete the entry → a second run → delete the saved CV →
    delete the account. Then: no marker in any log record, API-published domain event, Sentry
    envelope or Redis key; one line per flow names its id."""
    email = f"{_marker('email').lower()}@example.com"
    filename = _marker("filename") + ".txt"
    label = _marker("label")
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
        # --- register ---------------------------------------------------------------------------
        token, seeded_id = await seed_user_and_sign_in(client, settings, email=email)
        headers = bearer(token)
        ids["user_id"] = str(seeded_id)

        # --- saved CV, labelled -----------------------------------------------------------------
        uploaded = await client.post(
            ME_BASE_CVS, files={"file": (filename, cv_text.encode(), "text/plain")}, headers=headers
        )
        assert uploaded.status_code == 201, uploaded.text
        ids["base_cv_id"] = str(uploaded.json()["id"])
        renamed = await client.patch(
            f"{ME_BASE_CVS}/{ids['base_cv_id']}", json={"label": label}, headers=headers
        )
        assert renamed.status_code == 200, renamed.text

        # --- account postings: paste, then fetch ------------------------------------------------
        mark = len(caplog.records)
        pasted = await client.post(
            ME_POSTINGS, json={"source": "pasted", "text": pasted_text}, headers=headers
        )
        assert pasted.status_code == 201, pasted.text
        ids["pasted_posting_id"] = str(pasted.json()["id"])
        assert lines_naming(ids["pasted_posting_id"], mark) >= 1, "posting: capture not live"
        fetched = await client.post(
            ME_POSTINGS, json={"source": "fetched", "url": fetched_url}, headers=headers
        )
        assert fetched.status_code == 201, fetched.text
        assert fetcher.calls == 1
        ids["fetched_posting_id"] = str(fetched.json()["id"])

        # --- a run -------------------------------------------------------------------------------
        forbidden_to_llm = {
            "the email": email,
            "the user id": ids["user_id"],
            "the CV id": ids["base_cv_id"],
            "the pasted posting id": ids["pasted_posting_id"],
            "the fetched posting id": ids["fetched_posting_id"],
            "the CV label": label,
            "the CV filename": filename,
            "the posting URL": fetched_url,
            "the posting title": fetched_title,
        }
        mark = len(caplog.records)
        run_id, call = await _work_a_run(
            client,
            session,
            settings,
            headers,
            {"base_cv_id": ids["base_cv_id"], "job_posting_id": ids["pasted_posting_id"]},
            _draft(run_cv, run_letter),
        )
        _assert_llm_saw_only_the_two_texts(
            call,
            cv_text=cv_text,
            posting_text=pasted_text,
            forbidden={**forbidden_to_llm, "the run id": run_id},
        )
        assert lines_naming(run_id, mark) >= 1, "run: capture not live"

        # --- edit, history, export, re-open -------------------------------------------------------
        reopened = await client.get(f"{ME_RUNS}/{run_id}", headers=headers)
        assert reopened.status_code == 200, reopened.text
        edited = await client.put(
            f"{ME_RUNS}/{run_id}/documents/cv",
            json={"content": edit_text, "expected_version": reopened.json()["version"]},
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        history = await client.get(ME_RUNS, headers=headers)
        assert history.status_code == 200, history.text
        requested_export = await client.post(
            f"{ME_RUNS}/{run_id}/exports", json={"document": "cv", "format": "pdf"}, headers=headers
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
        again = await client.get(f"{ME_RUNS}/{run_id}", headers=headers)
        assert again.status_code == 200, again.text

        # --- delete the entry ---------------------------------------------------------------------
        mark = len(caplog.records)
        erased_entry = await client.delete(f"{ME_RUNS}/{run_id}", headers=headers)
        assert erased_entry.status_code == 204, erased_entry.text
        assert lines_naming(run_id, mark) >= 1, "entry deletion: capture not live"

        # --- a second run, on the fetched posting -------------------------------------------------
        second_run_id, second_call = await _work_a_run(
            client,
            session,
            settings,
            headers,
            {"base_cv_id": ids["base_cv_id"], "job_posting_id": ids["fetched_posting_id"]},
            _draft(second_cv, second_letter),
        )
        _assert_llm_saw_only_the_two_texts(
            second_call,
            cv_text=cv_text,
            posting_text=fetched_text,
            forbidden={**forbidden_to_llm, "the run id": second_run_id},
        )

        # --- delete the saved CV, then the account --------------------------------------------------
        removed = await client.delete(f"{ME_BASE_CVS}/{ids['base_cv_id']}", headers=headers)
        assert removed.status_code == 204, removed.text
        mark = len(caplog.records)
        deleted = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": A_PASSWORD},
            headers={**headers, "Origin": settings.public_base_url},
        )
        assert deleted.status_code == 204, deleted.text
        assert lines_naming(ids["user_id"], mark) >= 1, "erasure: capture not live"

    # --- Sentry: the channel is live, and the flow's breadcrumbs ride on a final event -------------
    sentry_sdk.capture_message("QA53-sentry-probe")
    sentry_sdk.flush()
    sentry_text = "\n".join(sentry_envelopes)
    assert "QA53-sentry-probe" in sentry_text, "the Sentry capture is not live"

    # --- The claim ---------------------------------------------------------------------------------
    markers = {
        "the email": email,
        "the CV filename": filename,
        "the CV label": label,
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


# --- AC-53: erase-account on a second user, then a purge and an orphan sweep -------------------------

_PASSWORD_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")


@pytest_asyncio.fixture
async def committed_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Bound to the session-scoped engine: the CLI's and the purge's own composition roots open a
    fresh engine and read committed rows (2.2's fixture, same reason)."""
    async with async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)() as s:
        yield s


async def _committed_history(
    session: AsyncSession,
    files: LocalFileStore,
    owner: UserOwner | GuestOwner,
    now: datetime,
    marker: str,
) -> FileRef:
    """A CV, a posting and a run (all carrying `marker`) and one ready PDF export whose bytes carry
    it too — committed. Returns the export's key."""
    cv = extracted_cv(owner, now)
    await SqlAlchemyBaseCvRepository(session).add(cv)
    posting = pasted_posting(owner, now, marker=marker)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    run = succeeded_run(owner, now, base_cv_id=cv.id, job_posting_id=posting.id)
    await SqlAlchemyTailoringRunRepository(session).add(run)
    job = ready_export(owner, run, now, format=ExportFormat.PDF)
    await SqlAlchemyExportJobRepository(session).add(job)
    await session.commit()
    await files.put(job.storage_ref, f"%PDF {marker}".encode())
    return job.storage_ref


async def _committed_user(session: AsyncSession, now: datetime, email: str) -> UserId:
    users = SqlAlchemyUserRepository(session)
    user_id = users.next_identity()
    await users.add(
        User.register_with_password(
            id=user_id, email=EmailAddress.parse(email), password_hash=_PASSWORD_HASH, at=now
        )
    )
    await session.commit()
    return user_id


async def test_ac53_erase_account_purge_and_sweep_over_history_never_log_a_marker(
    settings: Settings,
    engine: AsyncEngine,
    committed_session: AsyncSession,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A kept user, an erased user and an expired guest each hold a history entry whose posting text
    and export bytes carry a marker; a true orphan carries one too. `erase-account` (the real
    `erase_account_command.erase_account`) takes the second user; a real purge takes the guest; a
    real sweep takes the orphan; the kept user's export survives. No marker, key or joined path in
    any log record; each run's own completion line present (the positive controls). Green on
    arrival: every entry point here is landed code — this pins it over 2.3's shapes."""
    assert_test_database(settings)
    local_settings = settings.model_copy(update={"upload_dir": tmp_path})
    files = LocalFileStore(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    kept_marker, erased_marker, guest_marker = (
        _marker("KEPT"),
        _marker("ERASED"),
        _marker("GUEST"),
    )
    erased_email = f"{_marker('erased-email').lower()}@example.com"

    kept_user = await _committed_user(committed_session, now, f"{uuid4().hex}@example.com")
    erased_user = await _committed_user(committed_session, now, erased_email)
    session_id = GuestSessionId(uuid4())
    await committed_session.execute(
        guest_session_table.insert().values(
            id=session_id,
            token_hash=uuid4().hex * 2,
            created_at=now - timedelta(hours=25),
            expires_at=now - timedelta(hours=1),
        )
    )
    await committed_session.commit()
    kept_ref = await _committed_history(
        committed_session, files, UserOwner(kept_user), now, kept_marker
    )
    erased_ref = await _committed_history(
        committed_session, files, UserOwner(erased_user), now, erased_marker
    )
    guest_ref = await _committed_history(
        committed_session, files, GuestOwner(session_id), now, guest_marker
    )
    orphan_ref = FileRef(key=f"{uuid4().hex[:2]}/{uuid4().hex[2:4]}/{uuid4()}.pdf")
    orphan_marker = _marker("ORPHAN")
    await files.put(orphan_ref, orphan_marker.encode())
    for ref in (kept_ref, orphan_ref):
        old = (now - timedelta(hours=60)).timestamp()
        os.utime(tmp_path / ref.key, (old, old))

    try:
        with caplog.at_level(logging.DEBUG):
            erased_exit = await erase_account_command.erase_account(
                local_settings, user_id=erased_user, dry_run=False
            )
            purge_exit = await purge_command._purge_guests(
                local_settings, dry_run=False, limit=None
            )
            sweep_exit = await purge_command._reclaim_orphans(
                local_settings, dry_run=False, limit=None, grace_hours=24
            )

        assert erased_exit == erase_account_command.EXIT_OK, caplog.text
        assert purge_exit == purge_command.EXIT_OK, caplog.text
        assert sweep_exit == purge_command.EXIT_OK, caplog.text
        assert str(erased_user.value) in caplog.text, "erase-account: capture not live"
        assert "retention.purge_completed" in caplog.text
        assert "retention.orphan_scan_completed" in caplog.text
        assert not (tmp_path / erased_ref.key).exists()
        assert not (tmp_path / guest_ref.key).exists()
        assert not (tmp_path / orphan_ref.key).exists()
        assert (tmp_path / kept_ref.key).exists(), "the kept user's export must survive"

        forbidden = {
            "the erased user's email": erased_email,
            "the kept user's posting text / bytes": kept_marker,
            "the erased user's posting text / bytes": erased_marker,
            "the guest's posting text / bytes": guest_marker,
            "the orphan's bytes": orphan_marker,
            **{
                f"a storage key ({ref.key})": ref.key
                for ref in (kept_ref, erased_ref, guest_ref, orphan_ref)
            },
            **{
                f"a joined path ({ref.key})": str(tmp_path / ref.key)
                for ref in (erased_ref, guest_ref, orphan_ref)
            },
        }
        for description, marker in forbidden.items():
            assert marker not in caplog.text, f"{description} reached a log record"
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                guest_session_table.delete().where(guest_session_table.c.id == session_id)
            )
            await conn.execute(
                user_table.delete().where(user_table.c.id.in_([kept_user, erased_user]))
            )
