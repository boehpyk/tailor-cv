"""Application tests for `ReviseTailoredDocument` (T6, RED).

**Why these fakes, not a real Postgres:** the same reason every other file in this package gives —
`infrastructure/persistence/mapping/tailoring/` has no `version`/revision columns wired yet (T8-T10),
so a test importing `conftest.py`'s `session`/`engine` fixtures would fail for a reason that has
nothing to do with `ReviseTailoredDocument`. This use case is tested against the ports it actually
depends on: `TailoringRunRepository` (`FakeTailoringRunRepository`) and `GuestSessionRepository`
(`FakeGuestSessionRepository`), composed exactly as production does — through the *real*
`GetTailoringRunForSession`, not a stub — because the whole point of that composition (per
`ReviseTailoredDocument`'s own docstring) is that the "not mine → 404, never 403" rule is exercised
for real here, the same argument `test_request_tailoring_run.py` makes for its own composed reads.

Every assertion below states what `ReviseTailoredDocument.__call__` **should** do per
technical-plan.md's "Application layer" §1 ("Flow") and feature-spec.md's failure contract rows
E-5 … E-9, never what the (currently `NotImplementedError`) code was observed doing. Because the
skeleton's `__call__` body is an unconditional `raise NotImplementedError`, every test below is
expected to fail on that line: a real red, not a vacuous pass, and not an `ImportError`.

**AC-8** (two concurrent worker deliveries of one `queued` run) and **AC-9** (the sweep tolerating a
conflict mid-batch) are *not* here — they exercise `ExecuteTailoringRun` and
`AbandonStaleTailoringRuns`, not this use case, and live in `test_execute_tailoring_run.py` and
`test_abandon_stale_tailoring_runs.py` respectively, alongside the fixtures and helpers they already
share with the rest of each file.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRunForSession
from tailorcraft.application.tailoring.revise_tailored_document import (
    ReviseCoverLetterCommand,
    ReviseCvCommand,
    ReviseTailoredDocument,
)
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.events import DomainEvent, EventPublisherPort
from tailorcraft.domain.tailoring.errors import (
    TailoredDocumentVersionConflict,
    TailoringRunConcurrentlyModified,
    TailoringRunNotEditable,
    TailoringRunNotFound,
    TailoringRunNotOwnedBySession,
)
from tailorcraft.domain.tailoring.events import TailoredDocumentRevised
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocumentKind,
    TailoredDocuments,
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeGuestSessionRepository,
    FakeTailoringRunRepository,
    RecordingEventPublisher,
    create_active_session,
)

# --- Test helpers --------------------------------------------------------------------------------

# Matches the `clock` fixture in `tests/conftest.py` (2026-09-04T12:00:00Z) exactly, so every helper
# below can build instants relative to "now" without threading the fixture through each one.
_CLOCK_NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def _a_documents() -> TailoredDocuments:
    """The model's original draft — never touched by a revision (TR-11)."""
    return TailoredDocuments(cv=TailoredCv("d" * 400), cover_letter=CoverLetter("d" * 200))


def _a_metrics() -> LlmCallMetrics:
    return LlmCallMetrics(
        model=ModelName("gemini-2.5-flash"),
        prompt_version=PromptVersion("v1"),
        prompt_tokens=1_200,
        completion_tokens=800,
        duration_ms=4_300,
    )


def _revised_cv(marker: str = "z") -> TailoredCv:
    """450 non-whitespace characters — comfortably past `TailoredCv`'s 400-character floor."""
    return TailoredCv(marker * 450)


def _revised_cover_letter(marker: str = "w") -> CoverLetter:
    """250 non-whitespace characters — comfortably past `CoverLetter`'s 200-character floor."""
    return CoverLetter(marker * 250)


def _succeeded_run(*, session_id: GuestSessionId) -> TailoringRun:
    """A `succeeded` run — the only status any revision is legal from (TR-9) — reached the only
    legal way, through `request` → `mark_started` → `mark_succeeded`, never by touching a private
    attribute. Every instant is comfortably before `clock.now()` (`_CLOCK_NOW`), so a revision made
    *now* always satisfies TR-4's `at >= completed_at`."""
    run = TailoringRun.request(
        id=TailoringRunId(value=uuid4()),
        guest_session_id=session_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=_CLOCK_NOW - timedelta(minutes=15),
    )
    run.mark_started(_CLOCK_NOW - timedelta(minutes=10))
    run.mark_succeeded(_a_documents(), _a_metrics(), _CLOCK_NOW - timedelta(minutes=5))
    return run


def _queued_run(*, session_id: GuestSessionId) -> TailoringRun:
    return TailoringRun.request(
        id=TailoringRunId(value=uuid4()),
        guest_session_id=session_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=_CLOCK_NOW - timedelta(minutes=5),
    )


def _running_run(*, session_id: GuestSessionId) -> TailoringRun:
    run = _queued_run(session_id=session_id)
    run.mark_started(_CLOCK_NOW - timedelta(minutes=4))
    return run


def _failed_run(*, session_id: GuestSessionId) -> TailoringRun:
    run = _running_run(session_id=session_id)
    run.mark_failed(TailoringFailureReason.LLM_ERROR, _CLOCK_NOW - timedelta(minutes=3))
    return run


def _use_case(
    runs: FakeTailoringRunRepository,
    sessions: FakeGuestSessionRepository,
    events: EventPublisherPort,
    clock: FixedClock,
) -> ReviseTailoredDocument:
    get_run = GetTailoringRunForSession(runs, sessions, clock)
    return ReviseTailoredDocument(runs, get_run, events, clock)


class _SnapshotSavedCountOnFirstPublish:
    """`EventPublisherPort` that snapshots `len(saved)` — `FakeTailoringRunRepository.saved` — at the
    instant `publish` first runs, the same technique `test_abandon_stale_tailoring_runs.py`'s
    `_SnapshotSaveCallsOnFirstPublish` uses to prove a sweep's save-then-publish order. A nonzero
    snapshot is positive evidence the save had already landed by the time anything was announced,
    rather than only an end-state check that would pass even if publish ran first.
    """

    def __init__(self, saved: list[TailoringRun]) -> None:
        self._saved = saved
        self.published: list[DomainEvent] = []
        self.saved_count_at_first_publish: int | None = None

    async def publish(self, *events: DomainEvent) -> None:
        if self.saved_count_at_first_publish is None:
            self.saved_count_at_first_publish = len(self._saved)
        self.published.extend(events)


# --- 1. Happy path: both commands ------------------------------------------------------------------


async def test_revising_the_cv_bumps_the_version_and_publishes_after_the_save(
    clock: FixedClock,
) -> None:
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = _succeeded_run(session_id=session.id)
    await runs.add(run)
    original_version = run.version
    original_documents = run.current_documents
    assert original_documents is not None
    original_cover_letter_text = original_documents.cover_letter
    events = _SnapshotSavedCountOnFirstPublish(runs.saved)
    use_case = _use_case(runs, sessions, events, clock)
    new_cv = _revised_cv()

    cmd = ReviseCvCommand(
        tailoring_run_id=run.id,
        guest_session_id=session.id,
        content=new_cv,
        expected_version=original_version,
    )

    result = await use_case(cmd)

    assert result.version == original_version + 1
    assert result.current_documents is not None
    assert result.current_documents.cv == new_cv
    # the other document is untouched by a CV revision
    assert result.current_documents.cover_letter == original_cover_letter_text
    # TR-11: the draft is never overwritten by a revision
    assert result.documents is not None
    assert result.documents.cv != new_cv

    assert runs.saved == [run]

    revised_events = [e for e in events.published if isinstance(e, TailoredDocumentRevised)]
    assert len(revised_events) == 1
    event = revised_events[0]
    assert event.tailoring_run_id == run.id
    assert event.kind is TailoredDocumentKind.CV
    assert event.version == original_version + 1
    assert event.character_count == new_cv.character_count
    # save-before-publish: the save had already landed by the moment publish first ran
    assert events.saved_count_at_first_publish == 1


async def test_revising_the_cover_letter_bumps_the_version_and_publishes_after_the_save(
    clock: FixedClock,
) -> None:
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = _succeeded_run(session_id=session.id)
    await runs.add(run)
    original_version = run.version
    original_documents = run.current_documents
    assert original_documents is not None
    original_cv_text = original_documents.cv
    events = _SnapshotSavedCountOnFirstPublish(runs.saved)
    use_case = _use_case(runs, sessions, events, clock)
    new_letter = _revised_cover_letter()

    cmd = ReviseCoverLetterCommand(
        tailoring_run_id=run.id,
        guest_session_id=session.id,
        content=new_letter,
        expected_version=original_version,
    )

    result = await use_case(cmd)

    assert result.version == original_version + 1
    assert result.current_documents is not None
    assert result.current_documents.cover_letter == new_letter
    # the other document is untouched by a cover-letter revision
    assert result.current_documents.cv == original_cv_text
    # TR-11: the draft is never overwritten by a revision
    assert result.documents is not None
    assert result.documents.cover_letter != new_letter

    assert runs.saved == [run]

    revised_events = [e for e in events.published if isinstance(e, TailoredDocumentRevised)]
    assert len(revised_events) == 1
    event = revised_events[0]
    assert event.tailoring_run_id == run.id
    assert event.kind is TailoredDocumentKind.COVER_LETTER
    assert event.version == original_version + 1
    assert event.character_count == new_letter.character_count
    assert events.saved_count_at_first_publish == 1


# --- 2. E-5: an expired or unknown guest session ----------------------------------------------------


async def test_expired_guest_session_raises_guest_session_expired_and_writes_nothing(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="expired-session-token-hash".ljust(64, "0"),
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    runs = FakeTailoringRunRepository()
    run = _succeeded_run(session_id=expired.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, sessions, events, clock)
    cmd = ReviseCvCommand(
        tailoring_run_id=run.id,
        guest_session_id=expired.id,
        content=_revised_cv(),
        expected_version=run.version,
    )

    with pytest.raises(GuestSessionExpired):
        await use_case(cmd)

    assert runs.saved == []
    assert events.published == []


async def test_unknown_guest_session_raises_guest_session_not_found_and_writes_nothing(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    runs = FakeTailoringRunRepository()
    unknown_session_id = GuestSessionId(value=uuid4())
    run = _succeeded_run(session_id=unknown_session_id)
    await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, sessions, events, clock)
    cmd = ReviseCvCommand(
        tailoring_run_id=run.id,
        guest_session_id=unknown_session_id,
        content=_revised_cv(),
        expected_version=run.version,
    )

    with pytest.raises(GuestSessionNotFound):
        await use_case(cmd)

    assert runs.saved == []
    assert events.published == []


# --- 3. E-6: the run does not exist, or belongs to another session ---------------------------------


async def test_unknown_run_id_raises_tailoring_run_not_found_and_writes_nothing(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()  # empty: no run was ever added
    events = RecordingEventPublisher()
    use_case = _use_case(runs, sessions, events, clock)
    cmd = ReviseCvCommand(
        tailoring_run_id=TailoringRunId(value=uuid4()),
        guest_session_id=session.id,
        content=_revised_cv(),
        expected_version=1,
    )

    with pytest.raises(TailoringRunNotFound):
        await use_case(cmd)

    assert runs.saved == []
    assert events.published == []


async def test_run_owned_by_another_session_raises_tailoring_run_not_found_and_writes_nothing(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    owner_session = await create_active_session(sessions, clock, token_hash="a" * 64)
    caller_session = await create_active_session(sessions, clock, token_hash="b" * 64)
    runs = FakeTailoringRunRepository()
    run = _succeeded_run(session_id=owner_session.id)  # not the caller's
    await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, sessions, events, clock)
    cmd = ReviseCvCommand(
        tailoring_run_id=run.id,
        guest_session_id=caller_session.id,
        content=_revised_cv(),
        expected_version=run.version,
    )

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await use_case(cmd)

    # "not mine" and "does not exist" must be indistinguishable at this boundary (G-29/AC-14), and
    # the distinction survives only on `__cause__`, exactly as `GetTailoringRunForSession`'s own
    # docstring promises.
    assert isinstance(exc_info.value.__cause__, TailoringRunNotOwnedBySession)
    assert runs.saved == []
    assert events.published == []


# --- 4. E-7: the run is queued, running or failed ---------------------------------------------------


@pytest.mark.parametrize(
    ("build_run", "expected_status"),
    [
        pytest.param(_queued_run, TailoringRunStatus.QUEUED, id="queued"),
        pytest.param(_running_run, TailoringRunStatus.RUNNING, id="running"),
        pytest.param(_failed_run, TailoringRunStatus.FAILED, id="failed"),
    ],
)
async def test_revising_a_run_that_is_not_succeeded_raises_not_editable_and_writes_nothing(
    clock: FixedClock,
    build_run: Callable[..., TailoringRun],
    expected_status: TailoringRunStatus,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = build_run(session_id=session.id)
    await runs.add(run)
    original_version = run.version
    events = RecordingEventPublisher()
    use_case = _use_case(runs, sessions, events, clock)
    cmd = ReviseCvCommand(
        tailoring_run_id=run.id,
        guest_session_id=session.id,
        content=_revised_cv(),
        expected_version=original_version,
    )

    with pytest.raises(TailoringRunNotEditable) as exc_info:
        await use_case(cmd)

    assert exc_info.value.status is expected_status
    assert runs.saved == []
    assert events.published == []
    # the run itself is unchanged
    unchanged = await runs.get(run.id)
    assert unchanged.status is expected_status
    assert unchanged.version == original_version


# --- 5. E-8: a stale `expected_version` -------------------------------------------------------------


async def test_stale_expected_version_raises_version_conflict_carrying_both_numbers(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = _succeeded_run(session_id=session.id)
    await runs.add(run)
    current_version = run.version
    stale_expected_version = current_version - 1
    events = RecordingEventPublisher()
    use_case = _use_case(runs, sessions, events, clock)
    cmd = ReviseCvCommand(
        tailoring_run_id=run.id,
        guest_session_id=session.id,
        content=_revised_cv(),
        expected_version=stale_expected_version,
    )

    with pytest.raises(TailoredDocumentVersionConflict) as exc_info:
        await use_case(cmd)

    assert exc_info.value.expected_version == stale_expected_version
    assert exc_info.value.current_version == current_version
    assert runs.saved == []
    assert events.published == []
    unchanged = await runs.get(run.id)
    assert unchanged.version == current_version  # the run is unchanged


# --- 6. E-9: a concurrent write wins the race, and the loser's exception propagates -----------------


async def test_concurrent_modification_on_save_propagates_and_writes_nothing(
    clock: FixedClock,
) -> None:
    """The repository's `save` reports a database-level race — `conflict_on_save=1` is the in-memory
    stand-in for `StaleDataError` translated into `TailoringRunConcurrentlyModified` — and this use
    case, unlike `ExecuteTailoringRun`, does **not** catch it: the caller here is an HTTP request with
    a status code to answer (409), not a Celery task with an outcome vocabulary, so the honest thing
    is for the exception to reach the router's error boundary. `revise_cv` itself succeeds against the
    in-memory aggregate before the save is attempted — the domain cannot see another writer — so
    what is asserted is that nothing this use case controls (a persisted row, a published event)
    reflects that in-memory change once `save` has refused it.
    """
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository(conflict_on_save=1)
    run = _succeeded_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, sessions, events, clock)
    cmd = ReviseCvCommand(
        tailoring_run_id=run.id,
        guest_session_id=session.id,
        content=_revised_cv(),
        expected_version=run.version,
    )

    with pytest.raises(TailoringRunConcurrentlyModified):
        await use_case(cmd)

    assert runs.saved == []
    assert events.published == []
