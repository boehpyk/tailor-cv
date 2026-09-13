"""The `TailoringRun` aggregate: invariants TR-1...TR-7 from technical-plan.md, and the full legal-
transition table (AC-3) from the class docstring in `domain/tailoring/tailoring_run.py`.

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Every assertion here comes from the
invariant table and the transition table in the skeleton's docstrings, not from running the
(currently unimplemented) methods and recording what they did — a test written that way would have
no source of truth independent of the code it is meant to guard.

What the aggregate *records* lives in the sibling `test_events.py`, mirroring the `intake`/`posting`
split: "what state did this transition produce?" and "what fact did it announce to every subscriber
and every log line?" have different reasons to fail, and AC-22 is a privacy assertion that deserves
to be findable on its own.

There is no other way into a `running`/`succeeded`/`failed` run than driving it through the legal
transitions from `request(...)` — that absence is TR-1/TR-6's whole point — so every fixture below is
built with the small helpers in the second section rather than by touching a private attribute.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.tailoring.errors import (
    TailoringAlreadyDecided,
    TailoringAlreadyStarted,
    TailoringNotRunning,
)
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

_RUN_ID = TailoringRunId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcde0"))
_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
_BASE_CV_ID = BaseCvId(value=UUID("22222222-2222-7222-8222-222222222222"))
_JOB_POSTING_ID = JobPostingId(value=UUID("33333333-3333-7333-8333-333333333333"))

# Whole-second, per ADR-0007: the `Clock` port truncates at the source so a database round trip can
# never change a value, and a test double that invented microseconds would fail an equality
# assertion after that round trip on a day nobody has time for it.
_REQUESTED_AT = datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC)
_STARTED_AT = _REQUESTED_AT + timedelta(seconds=5)
_COMPLETED_AT = _STARTED_AT + timedelta(seconds=20)


# --- builders: the only way into any non-`queued` status is the legal transitions themselves -----


def _requested(*, at: datetime = _REQUESTED_AT) -> TailoringRun:
    """The only constructor, so every other builder below starts here."""
    return TailoringRun.request(
        id=_RUN_ID,
        guest_session_id=_SESSION_ID,
        base_cv_id=_BASE_CV_ID,
        job_posting_id=_JOB_POSTING_ID,
        requested_at=at,
    )


def _running(
    *, requested_at: datetime = _REQUESTED_AT, started_at: datetime = _STARTED_AT
) -> TailoringRun:
    run = _requested(at=requested_at)
    run.mark_started(started_at)
    return run


def _documents() -> TailoredDocuments:
    return TailoredDocuments(cv=TailoredCv("a" * 400), cover_letter=CoverLetter("a" * 200))


def _metrics() -> LlmCallMetrics:
    return LlmCallMetrics(
        model=ModelName("gemini-2.5-flash"),
        prompt_version=PromptVersion("v1"),
        prompt_tokens=1_200,
        completion_tokens=800,
        duration_ms=4_300,
    )


def _succeeded() -> TailoringRun:
    run = _running()
    run.mark_succeeded(_documents(), _metrics(), _COMPLETED_AT)
    return run


def _failed() -> TailoringRun:
    """A `failed` run reached via `running`, so it carries a `started_at` — the shape the table's
    `succeeded`/`failed` rows both need to exercise. `test_mark_failed_is_legal_from_queued` covers
    the other way a run reaches `failed`."""
    run = _running()
    run.mark_failed(TailoringFailureReason.LLM_ERROR, _COMPLETED_AT)
    return run


def _run_in_status(status: TailoringRunStatus) -> TailoringRun:
    if status is TailoringRunStatus.QUEUED:
        return _requested()
    if status is TailoringRunStatus.RUNNING:
        return _running()
    if status is TailoringRunStatus.SUCCEEDED:
        return _succeeded()
    if status is TailoringRunStatus.FAILED:
        return _failed()
    raise AssertionError(f"no builder for {status!r}")  # pragma: no cover


# --- TR-1: request stores exactly one owner session, one base CV id, one job posting id ----------


def test_request_stores_the_owner_session_and_the_two_input_ids() -> None:
    run = _requested()

    assert run.id == _RUN_ID
    assert run.guest_session_id == _SESSION_ID
    assert run.base_cv_id == _BASE_CV_ID
    assert run.job_posting_id == _JOB_POSTING_ID
    assert run.requested_at == _REQUESTED_AT


def test_freshly_requested_run_is_queued_with_no_outcome_yet() -> None:
    """The state right after `request()`, before any transition has run."""
    run = _requested()

    assert run.status is TailoringRunStatus.QUEUED
    assert run.documents is None
    assert run.metrics is None
    assert run.failure_reason is None
    assert run.started_at is None
    assert run.completed_at is None


# --- AC-3: the full legal-transition table, one parametrized test per column ----------------------
#
# Twelve cells: four `from_status` rows crossed with the three transition methods. The `expected`
# parameter is `None` for a legal cell (the transition must succeed and change state) and a
# `DomainError` subclass for an illegal one (the transition must raise exactly that error and leave
# the run's status unchanged).


@pytest.mark.parametrize(
    ("from_status", "expected_error"),
    [
        pytest.param(TailoringRunStatus.QUEUED, None, id="queued-to-running"),
        pytest.param(TailoringRunStatus.RUNNING, TailoringAlreadyStarted, id="running"),
        pytest.param(TailoringRunStatus.SUCCEEDED, TailoringAlreadyDecided, id="succeeded"),
        pytest.param(TailoringRunStatus.FAILED, TailoringAlreadyDecided, id="failed"),
    ],
)
def test_mark_started_transition_table(
    from_status: TailoringRunStatus, expected_error: type[Exception] | None
) -> None:
    """The `mark_started` column of AC-3's table: legal only from `queued`."""
    run = _run_in_status(from_status)

    if expected_error is None:
        run.mark_started(_STARTED_AT)
        assert run.status is TailoringRunStatus.RUNNING
        assert run.started_at == _STARTED_AT
    else:
        with pytest.raises(expected_error):
            run.mark_started(_STARTED_AT)


@pytest.mark.parametrize(
    ("from_status", "expected_error"),
    [
        pytest.param(TailoringRunStatus.QUEUED, TailoringNotRunning, id="queued"),
        pytest.param(TailoringRunStatus.RUNNING, None, id="running-to-succeeded"),
        pytest.param(TailoringRunStatus.SUCCEEDED, TailoringAlreadyDecided, id="succeeded"),
        pytest.param(TailoringRunStatus.FAILED, TailoringAlreadyDecided, id="failed"),
    ],
)
def test_mark_succeeded_transition_table(
    from_status: TailoringRunStatus, expected_error: type[Exception] | None
) -> None:
    """The `mark_succeeded` column: legal only from `running` — there is no path from `queued`
    straight to two documents, because documents come from a call and a call is what `running`
    records."""
    run = _run_in_status(from_status)
    documents = _documents()
    metrics = _metrics()

    if expected_error is None:
        run.mark_succeeded(documents, metrics, _COMPLETED_AT)
        assert run.status is TailoringRunStatus.SUCCEEDED
        assert run.documents == documents
        assert run.metrics == metrics
        assert run.completed_at == _COMPLETED_AT
    else:
        with pytest.raises(expected_error):
            run.mark_succeeded(documents, metrics, _COMPLETED_AT)


@pytest.mark.parametrize(
    ("from_status", "expected_error"),
    [
        pytest.param(TailoringRunStatus.QUEUED, None, id="queued-to-failed"),
        pytest.param(TailoringRunStatus.RUNNING, None, id="running-to-failed"),
        pytest.param(TailoringRunStatus.SUCCEEDED, TailoringAlreadyDecided, id="succeeded"),
        pytest.param(TailoringRunStatus.FAILED, TailoringAlreadyDecided, id="failed"),
    ],
)
def test_mark_failed_transition_table(
    from_status: TailoringRunStatus, expected_error: type[Exception] | None
) -> None:
    """The `mark_failed` column: legal from **both** non-terminal statuses, unlike the other two
    transitions. `test_mark_failed_is_legal_from_queued` below restates the `queued` cell as its own
    named test — see its docstring for why that cell specifically earns one."""
    run = _run_in_status(from_status)

    if expected_error is None:
        run.mark_failed(TailoringFailureReason.LLM_ERROR, _COMPLETED_AT)
        assert run.status is TailoringRunStatus.FAILED
        assert run.failure_reason is TailoringFailureReason.LLM_ERROR
        assert run.completed_at == _COMPLETED_AT
    else:
        with pytest.raises(expected_error):
            run.mark_failed(TailoringFailureReason.LLM_ERROR, _COMPLETED_AT)


def test_mark_failed_is_legal_from_queued() -> None:
    """The one cell in the table a future reader is most likely to "tidy" into an error, so it gets
    its own named test rather than living only inside the parametrized table above.

    Legal on purpose: a run can fail before it ever starts, and there is exactly one way. The row is
    committed and *then* the task is published, so a broker that refuses the publish leaves a committed
    run that can never run. The router records it `failed`/`not_queued` (G-14) rather than leaving it
    `queued` forever, where the client would poll it until it gave up. `abandoned` is not a second
    way: it is recorded from `running`, by the stale-run sweep or by a late redelivery (see ADR-0014's
    amendment). This failure must not be recorded by first calling `mark_started` to satisfy the
    state machine, because
    `started_at` means "a worker began a call", and a `started_at` invented to get past a guard is a
    timestamp that lies to every latency measurement built on it. Asserting `started_at is None`
    below is the proof that this path never manufactures one.
    """
    run = _requested()

    run.mark_failed(TailoringFailureReason.NOT_QUEUED, _REQUESTED_AT)

    assert run.status is TailoringRunStatus.FAILED
    assert run.failure_reason is TailoringFailureReason.NOT_QUEUED
    assert run.started_at is None
    assert run.completed_at == _REQUESTED_AT


# --- TR-4: started_at >= requested_at, completed_at >= started_at (or >= requested_at from queued) -


def test_mark_started_earlier_than_requested_at_raises_invariant_violated() -> None:
    run = _requested()
    earlier = _REQUESTED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        run.mark_started(earlier)


def test_mark_started_at_the_same_instant_as_requested_at_is_allowed() -> None:
    """TR-4 is `started_at >= requested_at` — equal is explicitly not a violation, only strictly
    earlier is. A worker fast enough to pick up a run within the same whole second must not be
    punished for its speed."""
    run = _requested()

    run.mark_started(_REQUESTED_AT)

    assert run.started_at == _REQUESTED_AT


def test_mark_succeeded_earlier_than_started_at_raises_invariant_violated() -> None:
    run = _running()
    earlier = _STARTED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        run.mark_succeeded(_documents(), _metrics(), earlier)


def test_mark_succeeded_at_the_same_instant_as_started_at_is_allowed() -> None:
    run = _running()

    run.mark_succeeded(_documents(), _metrics(), _STARTED_AT)

    assert run.completed_at == _STARTED_AT


def test_mark_failed_from_running_earlier_than_started_at_raises_invariant_violated() -> None:
    run = _running()
    earlier = _STARTED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        run.mark_failed(TailoringFailureReason.LLM_ERROR, earlier)


def test_mark_failed_from_running_at_the_same_instant_as_started_at_is_allowed() -> None:
    run = _running()

    run.mark_failed(TailoringFailureReason.LLM_ERROR, _STARTED_AT)

    assert run.completed_at == _STARTED_AT


def test_mark_failed_from_queued_earlier_than_requested_at_raises_invariant_violated() -> None:
    """The case the skeleton settled deliberately and the spec left open, per `mark_failed`'s own
    docstring: when `started_at` is `None` (the run never started), the comparison falls back to
    `requested_at` — a `not_queued`/`abandoned` failure must not be recorded as happening before its
    own run was even requested."""
    run = _requested()
    earlier = _REQUESTED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        run.mark_failed(TailoringFailureReason.NOT_QUEUED, earlier)


def test_mark_failed_from_queued_at_the_same_instant_as_requested_at_is_allowed() -> None:
    run = _requested()

    run.mark_failed(TailoringFailureReason.NOT_QUEUED, _REQUESTED_AT)

    assert run.completed_at == _REQUESTED_AT


# --- TR-5: a succeeded run has both documents — unconstructable with only one ---------------------


def test_tailored_documents_cannot_be_constructed_with_only_one_document() -> None:
    """TR-5 restated at the point this aggregate relies on it: `mark_succeeded` takes a
    `TailoredDocuments`, and this is the type that makes "succeeded with one document" unconstructable
    rather than merely rejected. `test_value_objects.py` asserts the identical thing at the value
    object's own level; it earns a second assertion here because this is the invariant the transition
    table above exists to protect, and a reader of this file should not have to trust that it holds
    without seeing it proven."""
    with pytest.raises(TypeError):
        TailoredDocuments(cv=TailoredCv("a" * 400))  # type: ignore[call-arg]


def test_mark_succeeded_requires_documents() -> None:
    """The other half of TR-5: `mark_succeeded` itself has no parameter that could carry half a
    result — a caller with only a CV has nothing to pass. Green on arrival: it is Python's own
    parameter binding that raises here, before any method body runs, so this does not need a working
    `request()` to prove it — called through the class with a placeholder `self`, since the missing
    argument is what must fail and no attribute of `self` is ever read on the way to that error."""
    with pytest.raises(TypeError):
        TailoringRun.mark_succeeded(None, metrics=_metrics(), at=_COMPLETED_AT)  # type: ignore[call-arg,arg-type]


# --- TR-6: the three references and requested_at are immutable; there is no retry/rerun -----------


@pytest.mark.parametrize(
    "attribute",
    ["guest_session_id", "base_cv_id", "job_posting_id", "requested_at"],
)
def test_tailoring_run_references_and_requested_at_are_read_only(attribute: str) -> None:
    run = _requested()

    with pytest.raises(AttributeError):
        setattr(run, attribute, "a bare setter would let this through")


def test_tailoring_run_has_no_retry_or_rerun_method() -> None:
    """TR-6, the part worth recording rather than assuming: there is no `retry()` and no `rerun()`
    anywhere on this class. "Try again" creates a *new* run, so that what a run cost, when it ran,
    which model wrote it and what it produced stay one immutable fact. Absence is what this asserts
    against — a future `rerun()` added in slice 1.4 turns this test red instead of being caught only
    by a reviewer's memory of this docstring.

    Checked on the **class**, not an instance: the absence of a method is a fact about the class
    itself, so this needs no working `request()` to prove and is green on arrival."""
    assert not hasattr(TailoringRun, "retry")
    assert not hasattr(TailoringRun, "rerun")


# --- TR-7: a succeeded run always carries LlmCallMetrics ------------------------------------------


def test_mark_succeeded_requires_metrics() -> None:
    """TR-7 as a signature: `mark_succeeded` has no way to accept two documents without a cost to go
    with them. Green on arrival, for the same reason `test_mark_succeeded_requires_documents` is —
    called through the class with a placeholder `self` so it needs no working `request()`."""
    with pytest.raises(TypeError):
        TailoringRun.mark_succeeded(None, documents=_documents(), at=_COMPLETED_AT)  # type: ignore[call-arg,arg-type]


# --- AC-5: the mapped-class default-constructor hole ----------------------------------------------
#
# Both assertions below are green on arrival: `__init__` takes no arguments today, so Python's own
# signature check refuses both calls before any body runs. They exist to keep it that way — the hole
# they guard only opens once `registry.map_imperatively` is wired at T19, and by then this test must
# already exist and already pass, or the hole reopens silently the day someone deletes the "empty"
# constructor as dead code.


def test_tailoring_run_cannot_be_constructed_with_the_request_arguments() -> None:
    """`request` is the only constructor. A direct call — even with every argument `request` itself
    needs — must be refused, because a second way in is a second place TR-1 and TR-2 could be
    bypassed."""
    with pytest.raises(TypeError):
        TailoringRun(  # type: ignore[call-arg]
            id=_RUN_ID,
            guest_session_id=_SESSION_ID,
            base_cv_id=_BASE_CV_ID,
            job_posting_id=_JOB_POSTING_ID,
            requested_at=_REQUESTED_AT,
        )


def test_tailoring_run_cannot_be_constructed_via_the_mapped_attribute_names() -> None:
    """The hole slice 1.2 found and closed for `JobPosting`, pinned here before this class is ever
    mapped. `registry.map_imperatively` installs a default constructor accepting the **mapped**
    attribute names on any mapped class that defines no `__init__` of its own — so without the
    explicit no-argument `__init__` in the skeleton, `TailoringRun(_status=TailoringRunStatus.
    SUCCEEDED)` would be a second, uninvariant-checked way to build one: no owner session, no inputs,
    and a status that claims a decision nothing made. This test is what keeps that empty-looking
    `__init__` from being deleted as dead code."""
    with pytest.raises(TypeError):
        TailoringRun(_status=TailoringRunStatus.SUCCEEDED)  # type: ignore[call-arg]


# --- is_stale (G-25', V5b): the rule shared by ExecuteTailoringRun step 3 and the beat sweep,
# AbandonStaleTailoringRuns ---------------------------------------------------------------------
#
# `started_at is None` while `status is RUNNING` is folded to "stale" by the aggregate's own
# docstring, but that cell is not reachable through the public API: `mark_started` sets `status`
# and `started_at` together and nothing else ever writes either, so there is no legal path to a
# `RUNNING` run with no `started_at`. Recorded here rather than tested by reaching into
# `run._started_at` directly, which would violate the same TR-1/TR-6 guarantee every other builder
# in this file respects. See the RED commit body for the same note.

_STALE_AFTER = timedelta(seconds=300)


def test_running_run_started_longer_ago_than_the_window_is_stale() -> None:
    run = _running(started_at=_STARTED_AT)
    now = _STARTED_AT + _STALE_AFTER + timedelta(seconds=1)

    assert run.is_stale(now, _STALE_AFTER) is True


def test_running_run_started_exactly_at_the_window_is_not_stale() -> None:
    """Strict `>`: a run exactly `stale_after` old is still fresh. This is the same comparison
    `ExecuteTailoringRun` step 3 already makes (see `test_execute_tailoring_run.py`'s
    `test_running_run_past_the_stale_window_is_recorded_abandoned`, which advances 301 seconds past
    a 300-second window for the identical reason)."""
    run = _running(started_at=_STARTED_AT)
    now = _STARTED_AT + _STALE_AFTER

    assert run.is_stale(now, _STALE_AFTER) is False


def test_running_run_started_more_recently_than_the_window_is_not_stale() -> None:
    run = _running(started_at=_STARTED_AT)
    now = _STARTED_AT + timedelta(seconds=1)

    assert run.is_stale(now, _STALE_AFTER) is False


@pytest.mark.parametrize(
    "status",
    [TailoringRunStatus.QUEUED, TailoringRunStatus.SUCCEEDED, TailoringRunStatus.FAILED],
)
def test_non_running_run_is_never_stale_however_old(status: TailoringRunStatus) -> None:
    """`QUEUED` has no worker to have lost; `SUCCEEDED`/`FAILED` are decided once (TR-3) and a
    decided run is not stale, it is over — however long ago it was requested or completed."""
    run = _run_in_status(status)
    long_after = _COMPLETED_AT + timedelta(days=365)

    assert run.is_stale(long_after, _STALE_AFTER) is False
