"""Task tests for `infrastructure/tasks/tailoring.py::run_tailoring` (T35, written **after**).

`run_tailoring` is a Celery task, so it is a **synchronous** function that bridges into async
application code with `asyncio.run(_execute(...))` (its own module docstring, and
`infrastructure/tasks/container.py`'s). That bridge is exactly where this file's two approaches part
ways, and both are deliberate rather than an inconsistency:

1. **Where no visibility into this suite's own uncommitted data is needed** — inspecting the task
   object's declared retry behaviour (AC-9), and running it end to end for a run id that genuinely
   does not exist anywhere (AC-11, G-26) — the tests below call `run_tailoring(...)` **literally**,
   through its real `asyncio.run()` bridge, with `container.get_settings` patched to this suite's
   `settings` (so the fresh connection it opens lands on `tailorcraft_test`, never the dev database)
   and `container.GeminiLlm` patched to a `FakeLlm` that must never be called on this path. This is
   the one place in the suite a *second, genuinely separate* Postgres connection is opened, and it is
   safe only because nothing on this path needs to see a row this test's own transactional `session`
   fixture wrote: G-26's whole point is that the run is not there for *anyone* to find. A plain `def
   test_...` (not `async def`) is required for this — `pytest-asyncio`'s session-scoped loop is only
   ever *running* while an `async def` test's body executes, and `asyncio.run()` refuses to start a
   second loop on top of one that is already running (`RuntimeError: asyncio.run() cannot be called
   from a running event loop`); a synchronous test body runs with that loop idle, which is exactly
   where a *fresh* `asyncio.run()` may open a *second*, disposable loop of its own.

2. **Where the task must see a run this test created** (AC-10, G-27) — a second, genuinely separate
   connection opened inside `run_tailoring`'s own `asyncio.run()` cannot see this suite's
   transactional `session` (`tests/api/test_tailoring.py`'s module docstring names the identical
   problem for the API suite, and `_execute`'s own docstring documents the asyncpg loop-binding
   footgun a naive workaround would hit). So these tests call `_execute` — the exact coroutine
   `run_tailoring` awaits inside its `asyncio.run()` — **directly**, `await`ed from an already-async
   test, with `tailoring_use_case` (as imported into `infrastructure.tasks.tailoring`) patched to
   yield the worker's real `_build_use_case(settings, session)` bound to *this test's own* `session` —
   the identical technique `tests/api/test_tailoring.py::_run_worker` uses for the API suite, adapted
   one layer down to the task's own bridging function rather than the use case alone. This is still
   the real production code the Celery task calls; only the sync/async bridge itself — which is
   incompatible with a rolled-back test transaction by construction, not by an oversight — is not
   re-exercised for these two tests.

Every run built below needs a REAL, persisted `BaseCv` (`extracted`) and `JobPosting`, not merely a
random UUID: `ExecuteTailoringRun` step 5 looks both up, and a run pointing at ids nobody wrote
resolves as `MISSING` for the wrong reason (the "session was purged" branch, not the one this file
means to test) rather than exercising the successful path.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy import text as sql_text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tailorcraft.application.tailoring.execute_tailoring_run import ExecuteTailoringRunOutcome
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import CvContentType, ExtractedText, OriginalFilename
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.errors import TailoringRunConcurrentlyModified
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoredDraft,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock

# Importing these four mapping modules is what runs `mapper_registry.map_imperatively(...)` for
# `GuestSession`, `BaseCv`, `JobPosting` and `TailoringRun` as an import side effect — collection
# order alone cannot be trusted to do this first (unlike `make test`'s full run, this file may also
# be run alone via `make test file=...`, and then nothing else has imported them yet). Without it,
# each `repositories.*` module below fails at ITS OWN top level: they each build an
# `InstrumentedAttribute` cast against a private attribute (`GuestSession._id`, and so on) that does
# not exist until the mapping has registered it — `test_job_posting_repository.py`'s identical
# comment names the exact `AttributeError` this produces, found the hard way when `ruff --fix` once
# removed an import like this one as "unused".
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import (
    base_cv_table,  # noqa: F401
)
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import (
    job_posting_table,  # noqa: F401
)
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,  # noqa: F401
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
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
from tailorcraft.infrastructure.tasks import tailoring as tailoring_task_module
from tailorcraft.infrastructure.tasks.tailoring import run_tailoring
from tests.integration.fakes import FakeLlm

# --- Test helpers ----------------------------------------------------------------------------------


async def _persist_owner(
    session: AsyncSession, clock: FixedClock, *, token_hash: str
) -> GuestSession:
    repo = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=repo.next_identity(), token_hash=token_hash, at=clock.now(), ttl_hours=24
    )
    await repo.add(owner)
    return owner


async def _ready_run(
    session: AsyncSession, owner_id: GuestSessionId, clock: FixedClock
) -> TailoringRun:
    """A real, persisted, `queued` `TailoringRun` referencing a real, `extracted` `BaseCv` and a
    real `JobPosting` — everything `ExecuteTailoringRun` reads back off the row at steps 1 and 5."""
    cvs = SqlAlchemyBaseCvRepository(session)
    cv_id = cvs.next_identity()
    cv = BaseCv.upload(
        id=cv_id,
        guest_session_id=owner_id,
        original_filename=OriginalFilename("cv.txt"),
        content_type=CvContentType.TXT,
        size_bytes=1234,
        file=FileRef.for_base_cv(cv_id, CvContentType.TXT),
        uploaded_at=clock.now(),
    )
    cv.mark_extracted(ExtractedText("word " * 200), clock.now())
    await cvs.add(cv)

    postings = SqlAlchemyJobPostingRepository(session)
    posting = JobPosting.from_pasted_text(
        id=postings.next_identity(),
        guest_session_id=owner_id,
        text=JobPostingText("x" * 150),
        created_at=clock.now(),
    )
    await postings.add(posting)

    runs = SqlAlchemyTailoringRunRepository(session)
    run = TailoringRun.request(
        id=runs.next_identity(),
        guest_session_id=owner_id,
        base_cv_id=cv.id,
        job_posting_id=posting.id,
        requested_at=clock.now(),
    )
    await runs.add(run)
    await session.commit()
    return run


def _a_draft() -> TailoredDraft:
    return TailoredDraft(
        documents=TailoredDocuments(cv=TailoredCv("a" * 450), cover_letter=CoverLetter("b" * 250)),
        metrics=LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=111,
            completion_tokens=222,
            duration_ms=1234,
        ),
    )


def _a_draft_with_markers(cv_marker: str, letter_marker: str) -> TailoredDraft:
    """`_a_draft`, with each document's text built around a unique marker — for a test that must
    prove a specific fragment of text does or does not survive into an exception message or a log
    line, rather than merely that *some* draft was saved."""
    return TailoredDraft(
        documents=TailoredDocuments(
            cv=TailoredCv(f"{cv_marker} " + "a" * 450),
            cover_letter=CoverLetter(f"{letter_marker} " + "b" * 250),
        ),
        metrics=LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=111,
            completion_tokens=222,
            duration_ms=1234,
        ),
    )


def _bind_task_to_this_sessions_worker(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, session: AsyncSession, fake_llm: FakeLlm
) -> None:
    """The technique this module's docstring describes as approach 2: `_execute` — the coroutine
    `run_tailoring` hands to `asyncio.run()` — is left untouched; only the composition root it opens
    (`tailoring_use_case`) is replaced with one bound to *this test's own* session, so a run this
    test just committed is visible to it without opening a second, genuinely separate connection.
    """
    monkeypatch.setattr(tailoring_container, "GeminiLlm", lambda settings: fake_llm)

    @asynccontextmanager
    async def _fake_tailoring_use_case() -> AsyncIterator[tuple[Any, AsyncSession]]:
        yield tailoring_container._build_use_case(settings, session), session

    monkeypatch.setattr(tailoring_task_module, "tailoring_use_case", _fake_tailoring_use_case)


# --- AC-9: the task declares no Celery-level retry --------------------------------------------------


def test_the_task_declares_no_autoretry_for_retry_backoff_or_max_retries() -> None:
    """Inspection only — no execution needed. `run_tailoring` is a `celery.local.PromiseProxy`
    resolving to a `Task` subclass built by `@app.task(name=..., bind=False, ignore_result=True)`,
    which passes neither `autoretry_for` nor `retry_backoff`: Celery only ever installs those two
    attributes on a task when `autoretry_for` is given to the decorator
    (`celery.app.task.Task._add_autoretry_behaviour`), so their plain *absence* from the task object
    is itself the proof nothing configured them — there is no "off" value to compare against, only
    "was it ever set at all". `max_retries` is different: `Task` carries a class-level default (`3`)
    whether or not a task customises it, so the only way to prove THIS task did not override it is
    to compare against that same class default rather than a bare literal — a hardcoded `3` here
    would prove nothing about `run_tailoring` in particular, only that Celery's default happens to be
    3 today.

    A test asserting their absence is what turns "just add one for safety" into a red test rather
    than a silent, double-spending second retry layer (AC-9, ADR-0014 §6, the task's own docstring).
    """
    from celery.app.task import Task

    assert not hasattr(run_tailoring, "autoretry_for")
    assert not hasattr(run_tailoring, "retry_backoff")
    assert run_tailoring.max_retries == Task.max_retries


# --- AC-11 / G-26: invoked for real, through its own asyncio.run() bridge, for an id that is not ---
# --- anywhere — the one case genuinely safe to run against a second, separate connection -----------


def test_the_task_returns_none_and_does_not_raise_for_an_unknown_run_id(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    fake_llm = FakeLlm(_a_draft())
    # `container.get_settings()` is the module-cached, process-wide settings — pointed at the DEV
    # database by default. Patched here so the genuinely fresh connection this call opens lands on
    # `tailorcraft_test`, never the dev database (this agent's own standing rule).
    monkeypatch.setattr(tailoring_container, "get_settings", lambda: settings)
    monkeypatch.setattr(tailoring_container, "GeminiLlm", lambda settings: fake_llm)

    with caplog.at_level(logging.INFO):
        result = run_tailoring(str(uuid4()))

    assert result is None, "AC-11: the task must return None regardless of the outcome"
    assert fake_llm.calls == [], "an unknown run id must never reach the model (G-26)"
    assert "tailoring.run_missing" in caplog.text
    assert fake_llm.closed is True, (
        "V4: tailoring_use_case must close the adapter it built for this task, even on the MISSING "
        "path where the model was never called"
    )


# --- AC-11, closed: a run that SUCCEEDS must also return None through the real bridge -------------
# --- (gap found in review before commit, 2026-09-12 — the test above proves nothing has leaked ----
# --- when there is nothing TO leak; the actual hazard is a run that produced two real documents) --


def test_the_task_returns_none_on_a_run_that_succeeds(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, clock: FixedClock
) -> None:
    """The prior version of this file asserted `returns None` only for an *unknown* run id (G-26),
    which has no documents to lose in the first place. AC-11's real hazard is the opposite case: a
    run that **succeeds** is exactly the run with a tailored CV and a cover letter for
    `result_expires=3600` to park in Redis for an hour — outside Postgres, outside the 1.6 purge's
    reach, outside every retention promise this product makes. This test drives that case through
    `run_tailoring`'s own `asyncio.run()` bridge, for real, and asserts the documents it just paid
    for do not come back as the task's return value.

    **Why this needs its own connection, genuinely committed.** `run_tailoring` opens a fresh engine
    and a fresh Postgres connection inside its own `asyncio.run()`
    (`tasks/container.py::tailoring_use_case`). This suite's shared `connection` fixture only ever
    releases a SAVEPOINT inside one transaction that is never committed and always rolled back
    (`conftest.py::connection`'s own docstring: "no test needs to clean up after itself") — a second,
    genuinely different connection cannot see it, by ordinary Postgres read-committed isolation, not
    by any bug in either fixture. So this test deliberately does NOT use the `session` fixture the
    rest of this file relies on: it builds and disposes its own throwaway engine, twice — once to
    seed, once to clean up — the same "one engine per invocation" shape `tailoring_use_case` itself
    uses and for the identical reason (the module docstring's own footgun): reusing ONE engine's
    pooled connections across two separate `asyncio.run()` calls would hand the second call a
    connection whose asyncpg transport is still bound to the first call's already-closed loop —
    `RuntimeError: got Future attached to a different loop` (or, past teardown, "Event loop is
    closed") — measured by writing this test against the session-scoped `engine` fixture first and
    watching it fail exactly that way. Everything it writes is committed for real, and — because
    nothing here rolls back automatically — deleted again in a `finally`. Deleting the
    `identity_guest_session` row is enough: the base CV, the job posting and the run all cascade
    (AC-27), which is also what this test's cleanup is implicitly proving still holds.

    **Why this is a plain `def test_...`, not `async def`**, exactly as the unknown-run-id test above
    is: `asyncio.run()` refuses to start a second loop on top of one already running, and
    pytest-asyncio's session-scoped loop is only ever *running* while an `async def` test's own body
    executes — idle here, which is where a fresh `asyncio.run()` may open one of its own.
    """
    fake_llm = FakeLlm(_a_draft())
    # As in the test above: `container.get_settings()` is process-wide and points at the dev
    # database by default. Patched so the fresh connection `run_tailoring` opens lands on
    # `tailorcraft_test`.
    monkeypatch.setattr(tailoring_container, "get_settings", lambda: settings)
    monkeypatch.setattr(tailoring_container, "GeminiLlm", lambda settings: fake_llm)

    async def _seed_a_run_that_will_succeed() -> tuple[GuestSessionId, TailoringRunId]:
        seeding_engine = create_async_engine(settings.test_database_url, poolclass=None)
        try:
            async with AsyncSession(seeding_engine, expire_on_commit=False) as seeding_session:
                owner = await _persist_owner(seeding_session, clock, token_hash="9" * 64)
                run = await _ready_run(seeding_session, owner.id, clock)  # commits for real
                return owner.id, run.id
        finally:
            await seeding_engine.dispose()

    async def _forget(owner_id: GuestSessionId) -> None:
        cleanup_engine = create_async_engine(settings.test_database_url, poolclass=None)
        try:
            async with AsyncSession(cleanup_engine, expire_on_commit=False) as cleanup_session:
                await cleanup_session.execute(
                    delete(guest_session_table).where(guest_session_table.c.id == owner_id)
                )
                await cleanup_session.commit()
        finally:
            await cleanup_engine.dispose()

    owner_id, run_id = asyncio.run(_seed_a_run_that_will_succeed())
    try:
        result = run_tailoring(str(run_id.value))
    finally:
        asyncio.run(_forget(owner_id))

    assert result is None, "AC-11: a successful run must not return the documents it produced"
    assert len(fake_llm.calls) == 1, (
        "the model must actually have been called — the success path, not a skip or a miss"
    )
    assert fake_llm.closed is True, (
        "V4: tailoring_use_case must close the adapter it built for this task, even on the "
        "successful path"
    )


# --- AC-10: two invocations for one run make exactly one LLM call ----------------------------------


async def test_two_invocations_for_one_run_make_exactly_one_llm_call(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1" * 64)
    run = await _ready_run(session, owner.id, clock)
    fake_llm = FakeLlm(_a_draft())
    _bind_task_to_this_sessions_worker(monkeypatch, settings, session, fake_llm)

    first = await tailoring_task_module._execute(run.id)
    assert first is ExecuteTailoringRunOutcome.SUCCEEDED
    assert len(fake_llm.calls) == 1

    second = await tailoring_task_module._execute(run.id)

    assert second is ExecuteTailoringRunOutcome.SKIPPED
    assert len(fake_llm.calls) == 1, "AC-10: a second invocation must not call the model again"


# --- G-27: an already-decided run is skipped, no LLM call -------------------------------------------


async def test_an_already_decided_run_is_skipped_with_no_llm_call(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="2" * 64)
    run = await _ready_run(session, owner.id, clock)
    fake_llm = FakeLlm(_a_draft())
    _bind_task_to_this_sessions_worker(monkeypatch, settings, session, fake_llm)
    decided = await tailoring_task_module._execute(run.id)
    assert decided is ExecuteTailoringRunOutcome.SUCCEEDED
    assert len(fake_llm.calls) == 1

    # A second, fresh fake proves the second invocation reaches no `LlmPort` at all, not merely that
    # the first one's call count did not advance further.
    second_fake_llm = FakeLlm(_a_draft())
    _bind_task_to_this_sessions_worker(monkeypatch, settings, session, second_fake_llm)

    outcome = await tailoring_task_module._execute(run.id)

    assert outcome is ExecuteTailoringRunOutcome.SKIPPED
    assert second_fake_llm.calls == [], "G-27: an already-decided run must make no LLM call"


# --- G-28 / AC-21 (verify round 1): a real database-side failure recording a succeeded run must ---
# --- leak the tailored documents through neither the escaping exception's message nor the logs ----


async def test_a_real_database_failure_saving_a_succeeded_run_leaks_no_document_text(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """G-28: "Postgres unavailable inside the worker when recording a successful outcome" is left to
    escape `run_tailoring` on purpose. Celery 5.6.3 ACKs a task that raises
    (`task_acks_on_failure_or_timeout=True`), so nothing redelivers this message; the run is left
    `running`, and the stale-run sweep (G-25', `tasks/tailoring_sweep.py`) is the one recovery for
    it, a beat tick later. AC-21 promises that escape carries no fragment of the tailored CV or
    cover letter — not in the exception's own message, not in its rendered chain, not in a log line.

    A **real** database-side failure, not a hand-built exception: a `CHECK` constraint added to
    `tailoring_run` for the life of this test rejects any `tailored_cv` containing this test's own
    marker, so `CommittingTailoringRunRepository.save`'s `UPDATE` — step 7's second transaction —
    fails at Postgres exactly the way a dropped connection or a real constraint violation would in
    production, with the failing value bound as a parameter.

    **Through the production engine, not a bypass.** `session` descends from `conftest.py`'s
    `connection` fixture, which as of verify-round-1 is opened on an `engine` built by
    `tailorcraft.infrastructure.persistence.database.create_engine` rather than a bare
    `create_async_engine` call — the same factory `tasks/container.py::tailoring_use_case` calls in
    production. A test whose failing write ran through some *other* engine could not tell whether
    `create_engine`'s own configuration was doing anything at all.

    **Strengthened after api-dev's round-1 `hide_parameters=True` attempt turned out not to be
    enough** (verify round 1): `hide_parameters=True` only deletes SQLAlchemy's own
    `[parameters: (...)]` line. The chained, driver-native exception underneath (`exc.__cause__`) is
    untouched and its own message still quotes the value — asyncpg's own text does this for a CHECK
    violation exactly as it does for the simpler cast failure in `test_database_engine.py` — so a
    test that only reads `str(exc)` can go green on a fix that leaves the real leak fully intact.
    `traceback.format_exception(exc)` renders the whole chain, which is what Celery's "raised
    unexpected: <exc>" line and Sentry's own capture actually walk.

    **The constraint's own name must survive** — a positive guard against the opposite failure mode,
    a sanitizer so aggressive it also destroys the one thing a person debugging a real production
    incident needs: which constraint fired, on which table.

    **Cleanup.** The constraint is added and dropped through this test's own `session`, which is
    bound to the SAVEPOINT the outer `connection` fixture always rolls back at teardown
    (`conftest.py`) — so it would disappear on its own even without the explicit `finally` below.
    The `finally` stays anyway, per this agent's standing instruction that a trigger or constraint a
    worker-path test creates must not be left for a second run to trip over: a `session.rollback()`
    first, because the failed flush inside `_execute` leaves this session's own transaction unusable
    for anything else until it is reset, and only then the `DROP CONSTRAINT`.
    """
    owner = await _persist_owner(session, clock, token_hash="4" * 64)
    run = await _ready_run(session, owner.id, clock)

    cv_marker = "QA_BREACH1_WORKER_CV_MARKER_7d1e9a"
    letter_marker = "QA_BREACH1_WORKER_LETTER_MARKER_2c4f1b"
    fake_llm = FakeLlm(_a_draft_with_markers(cv_marker, letter_marker))
    _bind_task_to_this_sessions_worker(monkeypatch, settings, session, fake_llm)

    constraint_name = "qa_breach1_worker_leak_guard"
    await session.execute(
        sql_text(
            f"ALTER TABLE tailoring_run ADD CONSTRAINT {constraint_name} "
            f"CHECK (tailored_cv IS NULL OR tailored_cv NOT LIKE '%{cv_marker}%')"
        )
    )
    await session.commit()

    try:
        with caplog.at_level(logging.INFO), pytest.raises(DBAPIError) as exc_info:
            await tailoring_task_module._execute(run.id)
    finally:
        await session.rollback()
        await session.execute(
            sql_text(f"ALTER TABLE tailoring_run DROP CONSTRAINT IF EXISTS {constraint_name}")
        )
        await session.commit()

    # The positive proof the assertions below cannot pass vacuously: the save must actually have
    # failed at the database, not merely returned without complaint. `isinstance`, not `type(...) is`
    # — the router's own G-13/G-14 handlers catch `SQLAlchemyError`, so a sanitizer that rebuilt this
    # as some unrelated type would silently turn those 503s into 500s, and this check would catch it.
    exc = exc_info.value
    assert isinstance(exc, DBAPIError)

    # Celery's "raised unexpected: <exc>" and Sentry's own capture both walk more than `str(exc)` —
    # `__cause__`/`__context__` too, which a bare `hide_parameters=True` leaves fully intact.
    rendered_chain = "".join(traceback.format_exception(exc))

    assert cv_marker not in str(exc), "AC-21: the tailored CV leaked into the exception's message"
    assert letter_marker not in str(exc), (
        "AC-21: the cover letter leaked into the exception's message"
    )
    assert cv_marker not in rendered_chain, (
        "AC-21: the tailored CV leaked into the exception's rendered chain "
        "(__cause__/__context__) — this is what Celery logs and what Sentry walks"
    )
    assert letter_marker not in rendered_chain, (
        "AC-21: the cover letter leaked into the exception's rendered chain "
        "(__cause__/__context__) — this is what Celery logs and what Sentry walks"
    )
    assert cv_marker not in caplog.text, "AC-21: the tailored CV leaked into a log line"
    assert letter_marker not in caplog.text, "AC-21: the cover letter leaked into a log line"

    assert constraint_name in str(exc), (
        "debuggability regression: the constraint's own name must survive sanitizing, or a real "
        "production incident becomes unattributable to the rule that actually fired"
    )

    # (verify round 2) `_withhold_driver_message` (infrastructure/persistence/database.py) rebuilds
    # the DBAPIError but still passes `wrapped.params` straight through, so the tailored CV and
    # cover letter this test bound as parameters survive as DATA on the exception object even though
    # every rendering checked above is clean. `DBAPIError.__reduce__` pickles `self.params`, and a
    # future `log.warning(params=exc.params)` or a Sentry `before_send` walking `vars(exc)` would
    # ship them regardless of str(exc) or the rendered chain.
    assert exc.params is None, (
        "no bound values may survive on the sanitized exception object — DBAPIError.__reduce__ "
        "pickles exc.params, and a future log line or Sentry before_send reading it back would leak "
        "the tailored CV or cover letter this test bound as parameters (G-28, AC-21)"
    )


# --- T16: `CommittingTailoringRunRepository.save` rolls back its own session on a conflict, and ----
# --- the session stays usable afterward -------------------------------------------------------------


async def test_the_committing_repositorys_save_rolls_back_on_a_conflict_and_the_session_stays_usable(
    settings: Settings,
    session: AsyncSession,
    connection: AsyncConnection,
    clock: FixedClock,
) -> None:
    """`CommittingTailoringRunRepository.save` (`tasks/container.py`) is the worker's own wrapper
    around `SqlAlchemyTailoringRunRepository`: every write commits, and its own docstring promises
    that a conflict rolls back the session **before** re-raising, because "nothing else would roll
    it back" in the worker — the task returns `SKIPPED` and its own closing commit would otherwise
    raise a second, unrelated error over the first.

    Driven with the identical two-`AsyncSession`-on-one-connection technique
    `test_tailoring_run_repository.py`'s own AC-7 test uses: `session2`'s copy is pre-loaded into
    its identity map *before* the winner (`session`) writes, so it is provably stale when the loser
    tries to save it through its own `CommittingTailoringRunRepository`.

    Two things are asserted beyond "it raises": that the SESSION `session2` is still usable
    afterward (a plain `get()` on it succeeds rather than raising `PendingRollbackError`, which is
    exactly what an un-rolled-back failed flush would do next), and that the row a fresh read sees
    is the WINNER's — the loser's `mark_started` never landed.
    """
    owner = await _persist_owner(session, clock, token_hash="9a" * 32)
    run = await _ready_run(session, owner.id, clock)  # queued, committed

    second_factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session2 = second_factory()
    repo2 = SqlAlchemyTailoringRunRepository(session2)
    # Pre-loaded into session2's identity map NOW, before the winner's write below — the same
    # technique (and the same reason) as the AC-7 persistence-level race test.
    stale_copy = await repo2.get(run.id)
    await session2.commit()  # releases the SAVEPOINT; expire_on_commit=False keeps the copy stale

    committing1 = tailoring_container.CommittingTailoringRunRepository(
        SqlAlchemyTailoringRunRepository(session), session
    )
    run.mark_started(clock.now())
    await committing1.save(run)

    committing2 = tailoring_container.CommittingTailoringRunRepository(repo2, session2)
    stale_copy.mark_started(clock.now())
    with pytest.raises(TailoringRunConcurrentlyModified):
        await committing2.save(stale_copy)

    # The session stays usable: a plain query on it succeeds rather than raising
    # `PendingRollbackError`, which is what a failed flush left un-rolled-back would do next.
    reread = await repo2.get(run.id)
    assert reread.status is TailoringRunStatus.RUNNING
    assert reread.started_at == run.started_at, "the row holds the WINNER's write, not the loser's"

    await session2.close()
    del stale_copy  # held until here on purpose (see the pre-load comment)


# --- T16: the task-level mirror of AC-8 — two deliveries of one queued run in flight at once -------


async def test_two_in_flight_deliveries_of_one_queued_run_through_the_task_function_make_one_llm_call(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: AsyncSession,
    connection: AsyncConnection,
    clock: FixedClock,
) -> None:
    """The task-level mirror of AC-8 (E-18): two deliveries of one `queued` run **in flight at the
    same time**, driven through `_execute` — the exact coroutine `run_tailoring`'s own
    `asyncio.run()` bridge awaits — rather than through the application-layer fakes
    (`test_execute_tailoring_run.py::test_two_concurrent_deliveries_the_loser_is_skipped_with_
    one_llm_call`, which drives the identical scenario against `FakeTailoringRunRepository`'s
    `conflict_on_save` switch instead of a real database).

    **Why this simulates rather than literally running two concurrent `asyncio.run()` calls, and
    why the simulation is not a weaker proof.** `run_tailoring` bridges into async code with a
    fresh `asyncio.run()` per invocation (`tasks/tailoring.py`'s own module docstring), and
    `asyncio.run()` is synchronous and blocking — two literal calls from one test cannot be in
    flight at the same time without two real OS threads, each spinning its own event loop and its
    own Postgres connection, which would prove only that two threads overlapped in wall-clock time.
    The actual race AC-8 describes is narrower and does not need threads to reproduce: two sessions
    each holding a `queued` copy of the same row before either writes. That is exactly what
    `test_tailoring_run_repository.py`'s own AC-7 persistence test reproduces without threads, by
    pre-loading a second session's copy into its identity map *before* the first session's write —
    the identical technique is used here, one layer up. `session2`'s copy is pre-loaded via a raw
    `repo2.get(run.id)` *before* the winner's `_execute` call; when the loser's own `_execute` call
    later does its internal `get()` (through a freshly-built `CommittingTailoringRunRepository`
    bound to the SAME `session2`), SQLAlchemy's identity map hands back the pre-loaded, stale object
    rather than re-querying — from the loser's point of view the winner's completed run never
    happened, which is precisely what two truly concurrent deliveries would each see.

    Per E-18's transition table the conflict fires at step 4 (recording `running`), **before** the
    model is ever called for the losing delivery — so the fake's total call count is 1, never 0 or
    2, and the loser's outcome is `SKIPPED`.
    """
    owner = await _persist_owner(session, clock, token_hash="9b" * 32)
    run = await _ready_run(session, owner.id, clock)
    fake_llm = FakeLlm(_a_draft())

    second_factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session2 = second_factory()
    repo2 = SqlAlchemyTailoringRunRepository(session2)
    # Pre-loaded into session2's identity map NOW, before the winner's delivery below ever writes.
    stale_copy = await repo2.get(run.id)
    await session2.commit()  # releases the SAVEPOINT; expire_on_commit=False keeps the copy stale

    _bind_task_to_this_sessions_worker(monkeypatch, settings, session, fake_llm)
    winner_outcome = await tailoring_task_module._execute(run.id)
    assert winner_outcome is ExecuteTailoringRunOutcome.SUCCEEDED
    assert len(fake_llm.calls) == 1

    _bind_task_to_this_sessions_worker(monkeypatch, settings, session2, fake_llm)
    loser_outcome = await tailoring_task_module._execute(run.id)

    assert loser_outcome is ExecuteTailoringRunOutcome.SKIPPED
    assert len(fake_llm.calls) == 1, "AC-8: the loser's delivery must never reach the model"

    await session2.close()
    del stale_copy  # held until here on purpose (see the pre-load comment)
