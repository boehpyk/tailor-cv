"""The `ExportJob` aggregate: invariants XJ-1...XJ-9 from technical-plan.md, and the full legal-
transition table (AC-3) from the class docstring in `domain/export/export_job.py`.

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Every assertion here comes from the
invariant table and the transition table in the skeleton's docstrings, not from running the
(currently unimplemented) methods and recording what they did — a test written that way would have
no source of truth independent of the code it is meant to guard.

There is no other way into a `rendering`/`ready`/`failed` job than driving it through the legal
transitions from `request(...)` — XJ-1/XJ-9's whole point — so every fixture below is built with the
small helpers in the second section rather than by touching a private attribute.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.export.errors import (
    ExportAlreadyDecided,
    ExportAlreadyStarted,
    ExportFormatNotQueued,
    ExportNotRendering,
    InvalidRunVersion,
)
from tailorcraft.domain.export.events import (
    ExportFailed,
    ExportReady,
    ExportRequested,
    ExportStarted,
)
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
)
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId

_JOB_ID = ExportJobId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcde0"))
_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
_RUN_ID = TailoringRunId(value=UUID("33333333-3333-7333-8333-333333333333"))
_DOCUMENT = TailoredDocumentKind.CV
_FORMAT = ExportFormat.PDF
_RUN_VERSION = 1

# Whole-second, per ADR-0007: the `Clock` port truncates at the source so a database round trip can
# never change a value, and a test double that invented microseconds would fail an equality
# assertion after that round trip on a day nobody has time for it.
_REQUESTED_AT = datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC)
_STARTED_AT = _REQUESTED_AT + timedelta(seconds=5)
_COMPLETED_AT = _STARTED_AT + timedelta(seconds=20)


# --- builders: the only way into any non-`queued` status is the legal transitions themselves -----


def _requested(
    *,
    at: datetime = _REQUESTED_AT,
    run_version: int = _RUN_VERSION,
    format: ExportFormat = _FORMAT,
) -> ExportJob:
    """The only constructor, so every other builder below starts here."""
    return ExportJob.request(
        id=_JOB_ID,
        guest_session_id=_SESSION_ID,
        tailoring_run_id=_RUN_ID,
        document=_DOCUMENT,
        format=format,
        run_version=run_version,
        requested_at=at,
    )


def _rendering(
    *, requested_at: datetime = _REQUESTED_AT, started_at: datetime = _STARTED_AT
) -> ExportJob:
    job = _requested(at=requested_at)
    job.mark_started(started_at)
    return job


def _ready() -> ExportJob:
    job = _rendering()
    job.mark_ready(byte_size=12_345, render_duration_ms=250, at=_COMPLETED_AT)
    return job


def _failed() -> ExportJob:
    """A `failed` job reached via `rendering`, so it carries a `started_at` — the shape the table's
    `ready`/`failed` rows both need to exercise. `test_mark_failed_is_legal_from_queued` covers the
    other way a job reaches `failed`."""
    job = _rendering()
    job.mark_failed(ExportFailureReason.RENDER_FAILED, _COMPLETED_AT)
    return job


def _job_in_status(status: ExportJobStatus) -> ExportJob:
    if status is ExportJobStatus.QUEUED:
        return _requested()
    if status is ExportJobStatus.RENDERING:
        return _rendering()
    if status is ExportJobStatus.READY:
        return _ready()
    if status is ExportJobStatus.FAILED:
        return _failed()
    raise AssertionError(f"no builder for {status!r}")  # pragma: no cover


# --- XJ-1: a job always has exactly one owner session, one run id, one document kind, one format --


def test_request_stores_the_owner_session_run_document_format_and_run_version() -> None:
    job = _requested()

    assert job.id == _JOB_ID
    assert job.guest_session_id == _SESSION_ID
    assert job.tailoring_run_id == _RUN_ID
    assert job.document is _DOCUMENT
    assert job.format is _FORMAT
    assert job.run_version == _RUN_VERSION
    assert job.requested_at == _REQUESTED_AT


def test_freshly_requested_job_is_queued_with_no_outcome_yet() -> None:
    """The state right after `request()`, before any transition has run."""
    job = _requested()

    assert job.status is ExportJobStatus.QUEUED
    assert job.failure_reason is None
    assert job.file_key is None
    assert job.byte_size is None
    assert job.render_duration_ms is None
    assert job.started_at is None
    assert job.completed_at is None
    assert job.version == 1


# --- XJ-2 / AC-2: request refuses an inline format, and refuses run_version < 1 (XJ-8) ------------


@pytest.mark.parametrize("format", [ExportFormat.MD, ExportFormat.TXT], ids=["md", "txt"])
def test_request_refuses_an_inline_format(format: ExportFormat) -> None:
    """An inline format has no job: it renders inside the request, with no row, no worker and no
    file (ADR-0005, ADR-0016 (a))."""
    with pytest.raises(ExportFormatNotQueued):
        _requested(format=format)


@pytest.mark.parametrize("run_version", [0, -1], ids=["zero", "negative"])
def test_request_refuses_a_run_version_below_one(run_version: int) -> None:
    """XJ-8: a run starts at version 1 and only counts up, so anything below it is a caller that
    never read the run."""
    with pytest.raises(InvalidRunVersion):
        _requested(run_version=run_version)


# --- AC-3: the full legal-transition table, one parametrized test per column ----------------------
#
# Twelve cells: four `from_status` rows crossed with the three transition methods. `expected_error`
# is `None` for a legal cell (the transition must succeed and change state) and a `DomainError`
# subclass for an illegal one (the transition must raise exactly that error).


@pytest.mark.parametrize(
    ("from_status", "expected_error"),
    [
        pytest.param(ExportJobStatus.QUEUED, None, id="queued-to-rendering"),
        pytest.param(ExportJobStatus.RENDERING, ExportAlreadyStarted, id="rendering"),
        pytest.param(ExportJobStatus.READY, ExportAlreadyDecided, id="ready"),
        pytest.param(ExportJobStatus.FAILED, ExportAlreadyDecided, id="failed"),
    ],
)
def test_mark_started_transition_table(
    from_status: ExportJobStatus, expected_error: type[Exception] | None
) -> None:
    """The `mark_started` column of AC-3's table: legal only from `queued`."""
    job = _job_in_status(from_status)

    if expected_error is None:
        job.mark_started(_STARTED_AT)
        assert job.status is ExportJobStatus.RENDERING
        assert job.started_at == _STARTED_AT
    else:
        with pytest.raises(expected_error):
            job.mark_started(_STARTED_AT)


@pytest.mark.parametrize(
    ("from_status", "expected_error"),
    [
        pytest.param(ExportJobStatus.QUEUED, ExportNotRendering, id="queued"),
        pytest.param(ExportJobStatus.RENDERING, None, id="rendering-to-ready"),
        pytest.param(ExportJobStatus.READY, ExportAlreadyDecided, id="ready"),
        pytest.param(ExportJobStatus.FAILED, ExportAlreadyDecided, id="failed"),
    ],
)
def test_mark_ready_transition_table(
    from_status: ExportJobStatus, expected_error: type[Exception] | None
) -> None:
    """The `mark_ready` column: legal only from `rendering` — there is no path from `queued`
    straight to a file, because a file comes from a render and `rendering` is what records that a
    render began."""
    job = _job_in_status(from_status)

    if expected_error is None:
        job.mark_ready(byte_size=12_345, render_duration_ms=250, at=_COMPLETED_AT)
        assert job.status is ExportJobStatus.READY
        assert job.byte_size == 12_345
        assert job.render_duration_ms == 250
        assert job.completed_at == _COMPLETED_AT
    else:
        with pytest.raises(expected_error):
            job.mark_ready(byte_size=12_345, render_duration_ms=250, at=_COMPLETED_AT)


@pytest.mark.parametrize(
    ("from_status", "expected_error"),
    [
        pytest.param(ExportJobStatus.QUEUED, None, id="queued-to-failed"),
        pytest.param(ExportJobStatus.RENDERING, None, id="rendering-to-failed"),
        pytest.param(ExportJobStatus.READY, ExportAlreadyDecided, id="ready"),
        pytest.param(ExportJobStatus.FAILED, ExportAlreadyDecided, id="failed"),
    ],
)
def test_mark_failed_transition_table(
    from_status: ExportJobStatus, expected_error: type[Exception] | None
) -> None:
    """The `mark_failed` column: legal from **both** non-terminal statuses, unlike the other two
    transitions. `test_mark_failed_is_legal_from_queued` below restates the `queued` cell as its own
    named test — see its docstring for why that cell specifically earns one."""
    job = _job_in_status(from_status)

    if expected_error is None:
        job.mark_failed(ExportFailureReason.RENDER_FAILED, _COMPLETED_AT)
        assert job.status is ExportJobStatus.FAILED
        assert job.failure_reason is ExportFailureReason.RENDER_FAILED
        assert job.completed_at == _COMPLETED_AT
    else:
        with pytest.raises(expected_error):
            job.mark_failed(ExportFailureReason.RENDER_FAILED, _COMPLETED_AT)


def test_mark_failed_is_legal_from_queued() -> None:
    """The one cell in the table a future reader is most likely to "tidy" into an error, so it gets
    its own named test rather than living only inside the parametrized table above.

    Legal on purpose: a job can fail before it ever starts, and there is exactly one way (X-22). The
    row is committed and *then* the task is published, so a broker that refuses the publish leaves a
    committed job that can never render. This failure must not be recorded by first calling
    `mark_started` to satisfy the state machine, because `started_at` means "a worker began a
    render", and one invented to get past a guard is a timestamp that lies to every latency
    measurement built on it. Asserting `started_at is None` below is the proof that this path never
    manufactures one.
    """
    job = _requested()

    job.mark_failed(ExportFailureReason.NOT_QUEUED, _REQUESTED_AT)

    assert job.status is ExportJobStatus.FAILED
    assert job.failure_reason is ExportFailureReason.NOT_QUEUED
    assert job.started_at is None
    assert job.completed_at == _REQUESTED_AT


# --- XJ-3: status == READY iff file_key/byte_size/render_duration_ms are all set; FAILED iff
# failure_reason is set. Never both, never neither. ------------------------------------------------


def test_ready_job_has_a_file_key_byte_size_and_duration_and_no_failure_reason() -> None:
    job = _ready()

    assert job.status is ExportJobStatus.READY
    assert job.file_key is not None
    assert job.byte_size == 12_345
    assert job.render_duration_ms == 250
    assert job.failure_reason is None


def test_failed_job_has_a_failure_reason_and_no_file_key_byte_size_or_duration() -> None:
    job = _failed()

    assert job.status is ExportJobStatus.FAILED
    assert job.failure_reason is ExportFailureReason.RENDER_FAILED
    assert job.file_key is None
    assert job.byte_size is None
    assert job.render_duration_ms is None


# --- XJ-5: started_at >= requested_at, completed_at >= started_at (or >= requested_at from queued) -


def test_mark_started_earlier_than_requested_at_raises_invariant_violated() -> None:
    job = _requested()
    earlier = _REQUESTED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        job.mark_started(earlier)


def test_mark_started_at_the_same_instant_as_requested_at_is_allowed() -> None:
    """XJ-5 is `started_at >= requested_at` — equal is explicitly not a violation, only strictly
    earlier is. A worker fast enough to pick up a job within the same whole second must not be
    punished for its speed."""
    job = _requested()

    job.mark_started(_REQUESTED_AT)

    assert job.started_at == _REQUESTED_AT


def test_mark_ready_earlier_than_started_at_raises_invariant_violated() -> None:
    job = _rendering()
    earlier = _STARTED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        job.mark_ready(byte_size=1, render_duration_ms=1, at=earlier)


def test_mark_ready_at_the_same_instant_as_started_at_is_allowed() -> None:
    job = _rendering()

    job.mark_ready(byte_size=1, render_duration_ms=1, at=_STARTED_AT)

    assert job.completed_at == _STARTED_AT


def test_mark_failed_from_rendering_earlier_than_started_at_raises_invariant_violated() -> None:
    job = _rendering()
    earlier = _STARTED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        job.mark_failed(ExportFailureReason.RENDER_FAILED, earlier)


def test_mark_failed_from_rendering_at_the_same_instant_as_started_at_is_allowed() -> None:
    job = _rendering()

    job.mark_failed(ExportFailureReason.RENDER_FAILED, _STARTED_AT)

    assert job.completed_at == _STARTED_AT


def test_mark_failed_from_queued_earlier_than_requested_at_raises_invariant_violated() -> None:
    """When `started_at` is `None` (the job never started), the comparison falls back to
    `requested_at` — a `not_queued`/`abandoned` failure must not be recorded as happening before its
    own job was even requested."""
    job = _requested()
    earlier = _REQUESTED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        job.mark_failed(ExportFailureReason.NOT_QUEUED, earlier)


def test_mark_failed_from_queued_at_the_same_instant_as_requested_at_is_allowed() -> None:
    job = _requested()

    job.mark_failed(ExportFailureReason.NOT_QUEUED, _REQUESTED_AT)

    assert job.completed_at == _REQUESTED_AT


# --- AC-6 / XJ-6: version is 1 after request, one more after every legal transition ----------------


def test_version_is_one_after_request() -> None:
    job = _requested()

    assert job.version == 1


def test_version_walk_through_queued_rendering_ready() -> None:
    job = _requested()
    assert job.version == 1

    job.mark_started(_STARTED_AT)
    assert job.version == 2

    job.mark_ready(byte_size=1, render_duration_ms=1, at=_COMPLETED_AT)
    assert job.version == 3


def test_version_walk_through_queued_rendering_failed() -> None:
    job = _requested()

    job.mark_started(_STARTED_AT)
    assert job.version == 2

    job.mark_failed(ExportFailureReason.RENDER_FAILED, _COMPLETED_AT)
    assert job.version == 3


def test_version_walk_through_queued_failed_directly() -> None:
    """The `not_queued` path: `request` (version 1) then `mark_failed` directly from `queued`
    (version 2) — `mark_started` is never called, so the walk is one transition shorter."""
    job = _requested()

    job.mark_failed(ExportFailureReason.NOT_QUEUED, _REQUESTED_AT)

    assert job.version == 2


# --- AC-5 / XJ-7: storage_ref is a pure function of id and format; mark_ready writes file_key from
# it; two jobs with the same id and format have equal refs -----------------------------------------


def test_storage_ref_is_available_before_any_render() -> None:
    """A pure function of `id` and `format`, available from construction — before `mark_ready` has
    ever been called."""
    job = _requested()

    assert job.storage_ref == FileRef.for_export(_JOB_ID, _FORMAT)


def test_file_key_matches_storage_ref_after_mark_ready() -> None:
    job = _ready()

    file_key = job.file_key
    assert file_key is not None
    assert job.storage_ref.key == file_key.key


def test_two_jobs_with_the_same_id_and_format_have_equal_storage_refs() -> None:
    """Constructed independently, both jobs compute the identical key — the retention hook 1.6
    consumes: 1.6 can find a job's file from a row, or reconstruct the key from the id alone, with
    no lookup table."""
    first = _requested()
    second = _requested()

    assert first.storage_ref == second.storage_ref


# --- XJ-9: the key fields and requested_at are immutable after creation ---------------------------


@pytest.mark.parametrize(
    "attribute",
    ["guest_session_id", "tailoring_run_id", "document", "format", "run_version", "requested_at"],
)
def test_export_job_key_fields_are_read_only(attribute: str) -> None:
    job = _requested()

    with pytest.raises(AttributeError):
        setattr(job, attribute, "a bare setter would let this through")


def test_export_job_has_no_retry_or_rerender_method() -> None:
    """XJ-9, the part worth recording rather than assuming: "export again" creates a *new* job at
    whatever the run's version is by then — a job is never re-pointed at a new version. Checked on
    the **class**, not an instance: the absence of a method is a fact about the class itself, so
    this needs no working `request()` to prove and is green on arrival."""
    assert not hasattr(ExportJob, "retry")
    assert not hasattr(ExportJob, "rerender")


# --- AC-2: the mapped-class default-constructor hole -----------------------------------------------
#
# Both assertions below are green on arrival: `__init__` takes no arguments today, so Python's own
# signature check refuses both calls before any body runs. They exist to keep it that way — the hole
# they guard only opens once `registry.map_imperatively` is wired, and by then this test must already
# exist and already pass, or the hole reopens silently the day someone deletes the "empty"
# constructor as dead code.


def test_export_job_cannot_be_constructed_with_the_request_arguments() -> None:
    """`request` is the only constructor. A direct call — even with every argument `request` itself
    needs — must be refused, because a second way in is a second place XJ-1 and XJ-2 could be
    bypassed."""
    with pytest.raises(TypeError):
        ExportJob(  # type: ignore[call-arg]
            id=_JOB_ID,
            guest_session_id=_SESSION_ID,
            tailoring_run_id=_RUN_ID,
            document=_DOCUMENT,
            format=_FORMAT,
            run_version=_RUN_VERSION,
            requested_at=_REQUESTED_AT,
        )


def test_export_job_cannot_be_constructed_via_the_mapped_attribute_names() -> None:
    """The hole `JobPosting` and `TailoringRun` were each found to have, pinned here before this
    class is ever mapped. `registry.map_imperatively` installs a default constructor accepting the
    **mapped** attribute names on any mapped class that defines no `__init__` of its own — so without
    the explicit no-argument `__init__` in the skeleton, `ExportJob(_status=ExportJobStatus.READY)`
    would be a second, uninvariant-checked way to build one: no owner session, no run, no format
    checked against XJ-2, and a status that claims a decision nothing made."""
    with pytest.raises(TypeError):
        ExportJob(_status=ExportJobStatus.READY)  # type: ignore[call-arg]


# --- is_stale: the rule shared by RenderExportJob and the beat sweep, AbandonStaleExportJobs -------

_STALE_AFTER = timedelta(seconds=120)


def test_rendering_job_started_longer_ago_than_the_window_is_stale() -> None:
    job = _rendering()
    now = _STARTED_AT + _STALE_AFTER + timedelta(seconds=1)

    assert job.is_stale(now, _STALE_AFTER) is True


def test_rendering_job_started_exactly_at_the_window_is_not_stale() -> None:
    """Strict `>`: a job exactly `stale_after` old is still fresh."""
    job = _rendering()
    now = _STARTED_AT + _STALE_AFTER

    assert job.is_stale(now, _STALE_AFTER) is False


def test_rendering_job_started_more_recently_than_the_window_is_not_stale() -> None:
    job = _rendering()
    now = _STARTED_AT + timedelta(seconds=1)

    assert job.is_stale(now, _STALE_AFTER) is False


@pytest.mark.parametrize(
    "status",
    [ExportJobStatus.QUEUED, ExportJobStatus.READY, ExportJobStatus.FAILED],
)
def test_non_rendering_job_is_never_stale_however_old(status: ExportJobStatus) -> None:
    """`QUEUED` has no worker to have lost; `READY`/`FAILED` are decided once (XJ-4) and a decided
    job is not stale, it is over — however long ago it was requested or completed."""
    job = _job_in_status(status)
    long_after = _COMPLETED_AT + timedelta(days=365)

    assert job.is_stale(long_after, _STALE_AFTER) is False


# --- was_requested_for: self.run_version == run_version --------------------------------------------


def test_was_requested_for_the_matching_run_version_is_true() -> None:
    job = _requested(run_version=3)

    assert job.was_requested_for(3) is True


def test_was_requested_for_a_different_run_version_is_false() -> None:
    job = _requested(run_version=3)

    assert job.was_requested_for(4) is False


# --- one event per transition; release_events() empties the buffer --------------------------------


def test_request_records_exactly_one_export_requested() -> None:
    job = _requested()

    events = job.release_events()

    assert len(events) == 1
    assert isinstance(events[0], ExportRequested)


def test_release_events_empties_the_buffer() -> None:
    """Called twice, the second call returns nothing, so a retried handler cannot publish the same
    fact twice."""
    job = _requested()
    job.release_events()

    assert job.release_events() == ()


def test_mark_started_records_exactly_one_export_started() -> None:
    job = _requested()
    job.release_events()  # discard ExportRequested so this test sees only what mark_started adds

    job.mark_started(_STARTED_AT)
    events = job.release_events()

    assert len(events) == 1
    assert isinstance(events[0], ExportStarted)


def test_mark_ready_records_exactly_one_export_ready() -> None:
    job = _rendering()
    job.release_events()

    job.mark_ready(byte_size=1, render_duration_ms=1, at=_COMPLETED_AT)
    events = job.release_events()

    assert len(events) == 1
    assert isinstance(events[0], ExportReady)


def test_mark_failed_records_exactly_one_export_failed() -> None:
    job = _rendering()
    job.release_events()

    job.mark_failed(ExportFailureReason.RENDER_FAILED, _COMPLETED_AT)
    events = job.release_events()

    assert len(events) == 1
    assert isinstance(events[0], ExportFailed)
