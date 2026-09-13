"""Application tests for `AbandonStaleTailoringRuns` (V5b, RED).

The beat sweep that delivers AC-12's amendment (G-25'): a `TailoringRun` that claims to be
`RUNNING` but whose worker was lost — a pool child killed, the main process SIGKILLed, a message
lost — is recorded `failed`/`abandoned` on a timer, because redelivery alone does not bring it back
(feature-spec.md, "Amended at /verify round 1").

**Why fakes, not a real Postgres**: the same reason `test_execute_tailoring_run.py` gives — this
use case is tested against the one port it depends on, `TailoringRunRepository`, via
`FakeTailoringRunRepository` (`tests/integration/fakes.py`), plus `RecordingEventPublisher` and
`FixedClock`.

**`list_stale_running` is now a member of `TailoringRunRepository`** (`domain/tailoring/ports.py`,
added at GREEN, V5c) — it was added to `FakeTailoringRunRepository` first, in the V5b commit this
file's tests were originally written against, ahead of the Protocol gaining the member; structural
typing made that order safe, since every test here calls the method on the concrete fake, never
through the Protocol type. Nothing below changed when the Protocol caught up.

Every assertion below states what `AbandonStaleTailoringRuns.__call__` **should** do per its own
skeleton docstring's "Flow (GREEN, V5c, implements it)" section and the amended feature-spec rows
G-25', G-35 and G-36 — never what the (currently `NotImplementedError`) code was observed doing.
Because the skeleton's `__call__` body is an unconditional `raise NotImplementedError`, every test
below is expected to fail on that line: a real red, not a vacuous pass, and not an `ImportError`.

**Save-before-publish** (item 1) needs a different technique than
`RecordingEventPublisher.repo_size_at_first_publish` uses elsewhere in this package
(`test_request_tailoring_run.py`, `test_capture_job_posting.py`): that mechanism proves an
**insert** landed before the first publish by watching the repository's row *count*, which never
changes here — the sweep `save`s a row the test's own setup already `add`ed. So this file instead
snapshots `FakeTailoringRunRepository.save_calls`, the same list `test_execute_tailoring_run.py`'s
`FakeLlm.on_call` hook snapshots to prove `running` is saved before the LLM is ever asked
(`_SnapshotSaveCallsOnFirstPublish`, below).

**G-36 and the drifting-adapter guard** (items 6 and 7) need a repository whose `list_stale_running`
hands back a candidate the real filter could never produce — an already-decided run, a run that is
still fresh — to prove the use case's own re-check of `TailoringRun.is_stale` is what catches it,
not the query. `_ForcedCandidatesRepository`, a tiny subclass local to this module, does that
without changing `FakeTailoringRunRepository`'s own contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import uuid4

from tailorcraft.application.tailoring.abandon_stale_tailoring_runs import (
    AbandonStaleTailoringRuns,
    AbandonStaleTailoringRunsResult,
)
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.events import DomainEvent, EventPublisherPort
from tailorcraft.domain.tailoring.errors import TailoringRunConcurrentlyModified
from tailorcraft.domain.tailoring.events import TailoringRunFailed
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeTailoringRunRepository, RecordingEventPublisher

# --- Test helpers ----------------------------------------------------------------------------------


def _a_session_id() -> GuestSessionId:
    """`AbandonStaleTailoringRuns` never looks a session up — it takes no `GuestSessionRepository`
    at all, same as `ExecuteTailoringRun` — so a fresh id with nothing behind it is exactly as good
    as a real one for every test here."""
    return GuestSessionId(value=uuid4())


def _requested_run(*, at: datetime) -> TailoringRun:
    return TailoringRun.request(
        id=TailoringRunId(value=uuid4()),
        guest_session_id=_a_session_id(),
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=at,
    )


def _running_run(*, requested_at: datetime, started_at: datetime) -> TailoringRun:
    """A `RUNNING` run, reached the only legal way — through `request` then `mark_started` — never
    by touching a private attribute (the same discipline `test_tailoring_run.py`'s builders keep)."""
    run = _requested_run(at=requested_at)
    run.mark_started(started_at)
    return run


def _a_documents() -> TailoredDocuments:
    return TailoredDocuments(cv=TailoredCv("a" * 400), cover_letter=CoverLetter("a" * 200))


def _a_metrics() -> LlmCallMetrics:
    return LlmCallMetrics(
        model=ModelName("gemini-2.5-flash"),
        prompt_version=PromptVersion("v1"),
        prompt_tokens=1_200,
        completion_tokens=800,
        duration_ms=4_300,
    )


def _use_case(
    runs: FakeTailoringRunRepository,
    events: EventPublisherPort,
    clock: FixedClock,
    *,
    stale_after_seconds: int = 300,
    batch_size: int = 100,
) -> AbandonStaleTailoringRuns:
    return AbandonStaleTailoringRuns(
        runs, events, clock, stale_after_seconds=stale_after_seconds, batch_size=batch_size
    )


def _failed_events_by_run(
    events: RecordingEventPublisher,
) -> dict[TailoringRunId, TailoringRunFailed]:
    """Every `TailoringRunFailed` this fake collected, keyed by the run it names. A `dict` rather
    than a list, so a test asserting "no event for this run" and "exactly one for that run" reads as
    a membership check rather than a list-comprehension each time."""
    return {e.tailoring_run_id: e for e in events.published if isinstance(e, TailoringRunFailed)}


class _SnapshotSaveCallsOnFirstPublish:
    """`EventPublisherPort` that records the length of a given `save_calls` log at the instant
    `publish` is first invoked — see this module's docstring for why
    `RecordingEventPublisher.repo_size_at_first_publish` cannot prove ordering for this use case's
    **update** path, and why this snapshots `FakeTailoringRunRepository.save_calls` instead, the
    same list `test_execute_tailoring_run.py`'s `on_call` hook uses for the same kind of proof.
    """

    def __init__(self, save_calls: list[TailoringRunStatus]) -> None:
        self._save_calls = save_calls
        self.published: list[DomainEvent] = []
        self.save_calls_at_first_publish: int | None = None

    async def publish(self, *events: DomainEvent) -> None:
        if self.save_calls_at_first_publish is None:
            self.save_calls_at_first_publish = len(self._save_calls)
        self.published.extend(events)


class _ForcedCandidatesRepository(FakeTailoringRunRepository):
    """A `FakeTailoringRunRepository` whose `list_stale_running` returns exactly the runs handed to
    it, bypassing the real filter — for G-36 and the drifting-adapter guard (items 6 and 7), where
    the point is to hand the use case a candidate its own query could never produce (an
    already-decided run; a fresh `RUNNING` run) and prove `TailoringRun.is_stale`'s re-check is what
    catches it, not the query. `add`/`get`/`save` are untouched, so the rest of the fake's contract
    — and `save_calls` — still behaves normally."""

    def __init__(self, candidates: Sequence[TailoringRun]) -> None:
        super().__init__()
        self._candidates = candidates

    async def list_stale_running(
        self, started_before: datetime, limit: int
    ) -> Sequence[TailoringRun]:
        return self._candidates[:limit]


class _ConflictingSaveRepository(FakeTailoringRunRepository):
    """A `FakeTailoringRunRepository` whose `save` raises `TailoringRunConcurrentlyModified` for one
    specific run id and behaves normally for every other — for AC-9/E-20, where the point is a
    conflict on the **middle** of three runs in listing order, sandwiched between two ordinary saves.

    `FakeTailoringRunRepository.conflict_on_save` (added in this same commit) cannot produce that
    shape on its own: it is a plain counter that fires on whichever `save` call comes next,
    regardless of which run it is for, so it can only make a conflict land first, not in the middle
    of a longer sequence of otherwise-successful saves. Targeting by id is what a real adapter's
    `WHERE version = :loaded` effectively does too — it fails whichever row's `UPDATE` no longer
    matches, never "the Nth write since startup."
    """

    def __init__(self, conflicting_run_id: TailoringRunId) -> None:
        super().__init__()
        self._conflicting_run_id = conflicting_run_id

    async def save(self, run: TailoringRun) -> None:
        if run.id == self._conflicting_run_id:
            raise TailoringRunConcurrentlyModified(run.id)
        await super().save(run)


# --- 1. A stale run is recorded failed/abandoned; the event is published only after the save -------


async def test_a_stale_running_run_is_recorded_failed_abandoned_with_the_event_published_after_save(
    clock: FixedClock,
) -> None:
    runs = FakeTailoringRunRepository()
    run = _running_run(
        requested_at=clock.now() - timedelta(seconds=310),
        started_at=clock.now() - timedelta(seconds=301),
    )
    await runs.add(run)
    events = _SnapshotSaveCallsOnFirstPublish(runs.save_calls)
    use_case = _use_case(runs, events, clock)

    result = await use_case()

    assert result == AbandonStaleTailoringRunsResult(abandoned=1, skipped=0, conflicts=0)
    stored = await runs.get(run.id)
    assert stored.status is TailoringRunStatus.FAILED
    assert stored.failure_reason is TailoringFailureReason.ABANDONED
    assert stored.completed_at == clock.now()

    failed_events = [e for e in events.published if isinstance(e, TailoringRunFailed)]
    assert len(failed_events) == 1
    assert failed_events[0].tailoring_run_id == run.id
    assert failed_events[0].reason is TailoringFailureReason.ABANDONED
    # save-before-publish: the terminal save had already happened by the moment publish first ran
    assert events.save_calls_at_first_publish == 1


# --- 2. A fresh running run is untouched; a stale one in the same batch proves the call ran ---------


async def test_a_fresh_running_run_is_untouched_while_a_stale_one_is_abandoned(
    clock: FixedClock,
) -> None:
    fresh_run = _running_run(
        requested_at=clock.now() - timedelta(seconds=10),
        started_at=clock.now() - timedelta(seconds=5),
    )
    stale_run = _running_run(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=301),
    )
    runs = FakeTailoringRunRepository()
    await runs.add(fresh_run)
    await runs.add(stale_run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, events, clock)

    result = await use_case()

    assert result == AbandonStaleTailoringRunsResult(abandoned=1, skipped=0, conflicts=0)
    unchanged = await runs.get(fresh_run.id)
    assert unchanged.status is TailoringRunStatus.RUNNING
    assert unchanged.failure_reason is None
    failed_by_run = _failed_events_by_run(events)
    assert fresh_run.id not in failed_by_run

    processed = await runs.get(stale_run.id)
    assert processed.status is TailoringRunStatus.FAILED
    assert processed.failure_reason is TailoringFailureReason.ABANDONED
    assert stale_run.id in failed_by_run


# --- 3. queued and terminal runs are untouched; a stale run in the same batch proves the call ran ---


async def test_queued_and_terminal_runs_are_untouched_while_a_stale_run_is_abandoned(
    clock: FixedClock,
) -> None:
    old_requested_at = clock.now() - timedelta(days=1)
    queued_run = _requested_run(at=old_requested_at)
    succeeded_run = _running_run(
        requested_at=old_requested_at, started_at=old_requested_at + timedelta(seconds=1)
    )
    succeeded_run.mark_succeeded(
        _a_documents(), _a_metrics(), old_requested_at + timedelta(seconds=2)
    )
    failed_run = _running_run(
        requested_at=old_requested_at, started_at=old_requested_at + timedelta(seconds=1)
    )
    failed_run.mark_failed(
        TailoringFailureReason.LLM_ERROR, old_requested_at + timedelta(seconds=2)
    )
    stale_run = _running_run(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=301),
    )
    runs = FakeTailoringRunRepository()
    for run in (queued_run, succeeded_run, failed_run, stale_run):
        await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, events, clock)

    result = await use_case()

    assert result == AbandonStaleTailoringRunsResult(abandoned=1, skipped=0, conflicts=0)
    assert (await runs.get(queued_run.id)).status is TailoringRunStatus.QUEUED
    assert (await runs.get(succeeded_run.id)).status is TailoringRunStatus.SUCCEEDED
    unchanged_failed = await runs.get(failed_run.id)
    assert unchanged_failed.status is TailoringRunStatus.FAILED
    assert unchanged_failed.failure_reason is TailoringFailureReason.LLM_ERROR  # not overwritten

    failed_by_run = _failed_events_by_run(events)
    assert queued_run.id not in failed_by_run
    assert succeeded_run.id not in failed_by_run
    assert failed_run.id not in failed_by_run

    processed = await runs.get(stale_run.id)
    assert processed.status is TailoringRunStatus.FAILED
    assert processed.failure_reason is TailoringFailureReason.ABANDONED
    assert stale_run.id in failed_by_run


# --- 4. The batch bound: the oldest are abandoned this call, the rest wait for the next -------------


async def test_batch_bound_abandons_the_oldest_and_leaves_the_rest_for_the_next_call(
    clock: FixedClock,
) -> None:
    oldest = _running_run(
        requested_at=clock.now() - timedelta(seconds=1_000),
        started_at=clock.now() - timedelta(seconds=900),
    )
    middle = _running_run(
        requested_at=clock.now() - timedelta(seconds=800),
        started_at=clock.now() - timedelta(seconds=700),
    )
    newest_stale = _running_run(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=301),
    )
    runs = FakeTailoringRunRepository()
    for run in (newest_stale, oldest, middle):  # added out of chronological order on purpose
        await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, events, clock, batch_size=2)

    first = await use_case()

    assert first == AbandonStaleTailoringRunsResult(abandoned=2, skipped=0, conflicts=0)
    assert (await runs.get(oldest.id)).status is TailoringRunStatus.FAILED
    assert (await runs.get(middle.id)).status is TailoringRunStatus.FAILED
    # the third-oldest, still genuinely stale, waits for the next tick — the batch bound, not staleness
    assert (await runs.get(newest_stale.id)).status is TailoringRunStatus.RUNNING

    second = await use_case()

    assert second == AbandonStaleTailoringRunsResult(abandoned=1, skipped=0, conflicts=0)
    assert (await runs.get(newest_stale.id)).status is TailoringRunStatus.FAILED


# --- 5. The boundary: a run exactly at the window is untouched --------------------------------------


async def test_a_run_exactly_at_the_stale_window_is_untouched(clock: FixedClock) -> None:
    """Strict `>`, matching `TailoringRun.is_stale`: a run exactly `stale_after_seconds` old is
    still fresh and is never even listed as a candidate."""
    boundary_run = _running_run(
        requested_at=clock.now() - timedelta(seconds=310),
        started_at=clock.now() - timedelta(seconds=300),  # exactly the window
    )
    stale_run = _running_run(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=301),
    )
    runs = FakeTailoringRunRepository()
    await runs.add(boundary_run)
    await runs.add(stale_run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, events, clock, stale_after_seconds=300)

    result = await use_case()

    assert result == AbandonStaleTailoringRunsResult(abandoned=1, skipped=0, conflicts=0)
    unchanged = await runs.get(boundary_run.id)
    assert unchanged.status is TailoringRunStatus.RUNNING
    assert unchanged.failure_reason is None

    processed = await runs.get(stale_run.id)
    assert processed.status is TailoringRunStatus.FAILED
    assert processed.failure_reason is TailoringFailureReason.ABANDONED


# --- 6. G-36: a run decided between load and mark is skipped without raising ------------------------


async def test_a_run_already_decided_between_load_and_mark_is_skipped_without_raising(
    clock: FixedClock,
) -> None:
    """The repository hands back a run that is already `succeeded`, as if a redelivered
    `ExecuteTailoringRun` reached step 3 and decided it first, between this tick's listing and its
    mark. `TailoringRun.is_stale` is `False` for any decided run (TR-3), so the use case must skip
    it without raising `TailoringAlreadyDecided` — the skeleton's docstring states there is
    deliberately no `except` for this, because the re-check alone already prevents the call. The
    rest of the batch (`stale_run`) still gets processed, proving the skip does not abort the tick.
    """
    decided_run = _running_run(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=395),
    )
    decided_run.mark_succeeded(_a_documents(), _a_metrics(), clock.now() - timedelta(seconds=350))
    stale_run = _running_run(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=301),
    )
    runs = _ForcedCandidatesRepository([decided_run, stale_run])
    await runs.add(decided_run)
    await runs.add(stale_run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, events, clock)

    result = await use_case()

    assert result == AbandonStaleTailoringRunsResult(abandoned=1, skipped=1, conflicts=0)
    unchanged = await runs.get(decided_run.id)
    assert unchanged.status is TailoringRunStatus.SUCCEEDED
    assert unchanged.completed_at == clock.now() - timedelta(seconds=350)  # untouched
    failed_by_run = _failed_events_by_run(events)
    assert decided_run.id not in failed_by_run

    processed = await runs.get(stale_run.id)
    assert processed.status is TailoringRunStatus.FAILED
    assert processed.failure_reason is TailoringFailureReason.ABANDONED
    assert stale_run.id in failed_by_run


# --- 7. Guard against a drifting adapter: a wrongly-returned fresh run is left untouched -------------


async def test_a_wrongly_returned_fresh_running_run_is_left_untouched_by_the_re_check(
    clock: FixedClock,
) -> None:
    """The repository wrongly hands back a run that is still fresh — its own query and
    `TailoringRun.is_stale` disagreeing, which a correct SQL adapter should never do. This is what
    makes the re-check a tested rule rather than decoration that only ever agrees with its own
    repository: the use case must leave the run untouched anyway."""
    fresh_run = _running_run(
        requested_at=clock.now() - timedelta(seconds=10),
        started_at=clock.now() - timedelta(seconds=5),
    )
    stale_run = _running_run(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=301),
    )
    runs = _ForcedCandidatesRepository([fresh_run, stale_run])
    await runs.add(fresh_run)
    await runs.add(stale_run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, events, clock)

    result = await use_case()

    assert result == AbandonStaleTailoringRunsResult(abandoned=1, skipped=1, conflicts=0)
    unchanged = await runs.get(fresh_run.id)
    assert unchanged.status is TailoringRunStatus.RUNNING
    assert unchanged.failure_reason is None
    failed_by_run = _failed_events_by_run(events)
    assert fresh_run.id not in failed_by_run

    processed = await runs.get(stale_run.id)
    assert processed.status is TailoringRunStatus.FAILED
    assert stale_run.id in failed_by_run


# --- 8. One instant per batch: every run abandoned on one call shares the same completed_at ----------


async def test_every_run_abandoned_in_one_batch_shares_the_same_completed_at(
    clock: FixedClock,
) -> None:
    """The use case reads `clock.now()` once per batch (skeleton docstring, flow step 1): every run
    it abandons on this call gets the identical `completed_at`, never a slightly later one per row
    as the loop progresses."""
    first_stale = _running_run(
        requested_at=clock.now() - timedelta(seconds=1_000),
        started_at=clock.now() - timedelta(seconds=900),
    )
    second_stale = _running_run(
        requested_at=clock.now() - timedelta(seconds=800),
        started_at=clock.now() - timedelta(seconds=700),
    )
    runs = FakeTailoringRunRepository()
    await runs.add(first_stale)
    await runs.add(second_stale)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, events, clock)

    result = await use_case()

    assert result == AbandonStaleTailoringRunsResult(abandoned=2, skipped=0, conflicts=0)
    completed_first = (await runs.get(first_stale.id)).completed_at
    completed_second = (await runs.get(second_stale.id)).completed_at
    assert completed_first == completed_second == clock.now()


# --- 9. AC-9/E-20: a conflicting save is counted and the batch continues past it ---------------------


async def test_a_conflicting_save_is_counted_and_the_batch_continues_to_the_next_run(
    clock: FixedClock,
) -> None:
    """AC-9 / E-20: a worker or a redelivery decided `middle` between this tick's read of it and the
    sweep's own attempt to write it — the repository's `save` raises `TailoringRunConcurrentlyModified`,
    exactly as the mapping's `version_id_col` mismatch does once T9 translates `StaleDataError` into
    it. The sweep must neither raise nor abort the batch over that: `middle` is counted in
    `conflicts` and left exactly as this tick found it (still `RUNNING` — whichever write actually
    landed stands, and this tick's did not), while `oldest` (processed before it) and `youngest`
    (processed after it, in the same tick) are both abandoned — proof the loop carries on past a
    conflict in the middle of the batch rather than stopping there.

    Against the unmodified `abandon_stale_tailoring_runs.py` this must go red: there is no
    `except TailoringRunConcurrentlyModified` around `save`, so the raise from `middle`'s save
    propagates out of `__call__` uncaught and `youngest`, listed after it, is never reached.
    """
    oldest = _running_run(
        requested_at=clock.now() - timedelta(seconds=1_000),
        started_at=clock.now() - timedelta(seconds=900),
    )
    middle = _running_run(
        requested_at=clock.now() - timedelta(seconds=800),
        started_at=clock.now() - timedelta(seconds=700),
    )
    youngest = _running_run(
        requested_at=clock.now() - timedelta(seconds=600),
        started_at=clock.now() - timedelta(seconds=500),
    )
    runs = _ConflictingSaveRepository(middle.id)
    for run in (oldest, middle, youngest):  # list_stale_running's contract: oldest started_at first
        await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(runs, events, clock)

    result = await use_case()

    assert result == AbandonStaleTailoringRunsResult(abandoned=2, skipped=0, conflicts=1)

    assert (await runs.get(oldest.id)).status is TailoringRunStatus.FAILED
    # The conflicting write never landed. Asserted on `saved`, not on `get(middle.id).status`: the
    # fake hands back the same object the sweep mutated, so its status is `FAILED` in memory even
    # though the save was refused — a real session would discard that object on rollback, and the
    # row would hold whatever the winning writer wrote. `saved` is the fake's record of what
    # actually reached the store, which is the fact AC-9 is about.
    assert middle not in runs.saved
    assert (await runs.get(youngest.id)).status is TailoringRunStatus.FAILED

    failed_by_run = _failed_events_by_run(events)
    assert oldest.id in failed_by_run
    assert middle.id not in failed_by_run  # its save never succeeded, so nothing to announce
    assert youngest.id in failed_by_run
