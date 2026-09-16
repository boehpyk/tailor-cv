"""`TailoringRun`'s two new transitions, `revise_cv` and `revise_cover_letter`, and the state they
touch: `version`, `current_documents`, and the `documents` draft they must never overwrite.

Slice 1.4 (workspace-progress-and-editor), ADR-0015. Pure domain tests: no I/O, no fixtures beyond
`parametrize`, no event loop, no mocks — the same discipline `test_tailoring_run.py` and
`test_events.py` hold for 1.3's three transitions, applied to the two this slice adds.

Every assertion below comes from technical-plan.md's extended transition table and invariants
TR-8...TR-11, and from feature-spec.md's AC-4...AC-6 — never from running `revise_cv` or
`revise_cover_letter`, which at the time of writing raise `NotImplementedError`, or from reading
`version`/`current_documents`, which do the same (docs/sdlc.md §2). A red on `NotImplementedError`
from one of those three is the expected, correct red for this file; a red on `ImportError` or
`AttributeError` would mean the T1 skeleton is incomplete and this file would need to go back to
`domain-modeler` unchanged.

Kept separate from `test_tailoring_run.py` rather than appended to it, so the diff that adds it is
reviewable on its own — the sibling file's docstring already explains why 1.3's transition table and
this one's live in the same class but not the same file for events (`test_events.py`); the same
reasoning applies here for revisions.
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
    TailoredDocumentVersionConflict,
    TailoringRunNotEditable,
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

_RUN_ID = TailoringRunId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcde0"))
_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
_BASE_CV_ID = BaseCvId(value=UUID("22222222-2222-7222-8222-222222222222"))
_JOB_POSTING_ID = JobPostingId(value=UUID("33333333-3333-7333-8333-333333333333"))

# Whole-second, per ADR-0007 — see test_tailoring_run.py's identical constants for the full reasoning.
_REQUESTED_AT = datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC)
_STARTED_AT = _REQUESTED_AT + timedelta(seconds=5)
_COMPLETED_AT = _STARTED_AT + timedelta(seconds=20)
_EDITED_AT = _COMPLETED_AT + timedelta(seconds=30)


# --- builders: the only way into any status, including a succeeded one, is the legal transitions ---


def _requested(*, at: datetime = _REQUESTED_AT) -> TailoringRun:
    return TailoringRun.request(
        id=_RUN_ID,
        guest_session_id=_SESSION_ID,
        base_cv_id=_BASE_CV_ID,
        job_posting_id=_JOB_POSTING_ID,
        requested_at=at,
    )


def _running() -> TailoringRun:
    run = _requested()
    run.mark_started(_STARTED_AT)
    return run


def _draft_cv() -> TailoredCv:
    return TailoredCv("a" * 400)


def _draft_letter() -> CoverLetter:
    return CoverLetter("a" * 200)


def _documents() -> TailoredDocuments:
    return TailoredDocuments(cv=_draft_cv(), cover_letter=_draft_letter())


def _metrics() -> LlmCallMetrics:
    return LlmCallMetrics(
        model=ModelName("gemini-2.5-flash"),
        prompt_version=PromptVersion("v1"),
        prompt_tokens=1_200,
        completion_tokens=800,
        duration_ms=4_300,
    )


def _succeeded(*, completed_at: datetime = _COMPLETED_AT) -> TailoringRun:
    """A run at `version == 3`: `request` (1) -> `mark_started` (2) -> `mark_succeeded` (3)."""
    run = _running()
    run.mark_succeeded(_documents(), _metrics(), completed_at)
    return run


def _failed_from_running() -> TailoringRun:
    run = _running()
    run.mark_failed(TailoringFailureReason.LLM_ERROR, _COMPLETED_AT)
    return run


def _run_not_editable(status: TailoringRunStatus) -> TailoringRun:
    """A run in one of the three statuses `revise_*` must refuse. Not named `_run_in_status` (the
    sibling file's helper): `SUCCEEDED` is deliberately absent here, because it is the one status
    where a revision is legal and it needs its own builder (`_succeeded`) with its own well-known
    version."""
    if status is TailoringRunStatus.QUEUED:
        return _requested()
    if status is TailoringRunStatus.RUNNING:
        return _running()
    if status is TailoringRunStatus.FAILED:
        return _failed_from_running()
    raise AssertionError(f"not an illegal-to-edit status: {status!r}")  # pragma: no cover


# A different value from the draft, so a test can tell "the revision landed" from "the draft leaked
# through unchanged" by comparing content rather than by trusting a version number alone.
def _revised_cv() -> TailoredCv:
    return TailoredCv("b" * 400)


def _revised_letter() -> CoverLetter:
    return CoverLetter("b" * 200)


# --- AC-4 / TR-8: version is 1 at request, and every legal transition bumps it by exactly one ------


def test_version_walk_through_the_success_lifecycle_and_two_revisions() -> None:
    """One run, walked through every step that can follow a request without failing: `request` (1),
    `mark_started` (2), `mark_succeeded` (3), `revise_cv` (4), `revise_cover_letter` (5). Asserted
    after every single step, so a transition that bumps by zero or by two is caught at the step it
    happens rather than only at the end."""
    run = _requested()
    assert run.version == 1

    run.mark_started(_STARTED_AT)
    assert run.version == 2

    run.mark_succeeded(_documents(), _metrics(), _COMPLETED_AT)
    assert run.version == 3

    run.revise_cv(_revised_cv(), expected_version=3, at=_EDITED_AT)
    assert run.version == 4

    run.revise_cover_letter(_revised_letter(), expected_version=4, at=_EDITED_AT)
    assert run.version == 5


def test_version_walk_through_failure_from_running() -> None:
    """The other legal walk through a non-terminal state: `request` (1), `mark_started` (2),
    `mark_failed` (3)."""
    run = _requested()
    assert run.version == 1

    run.mark_started(_STARTED_AT)
    assert run.version == 2

    run.mark_failed(TailoringFailureReason.LLM_ERROR, _COMPLETED_AT)
    assert run.version == 3


def test_version_walk_through_failure_from_queued() -> None:
    """The one cell where `mark_failed` is legal without `mark_started` ever running (G-14):
    `request` (1), `mark_failed` (2) — one bump, not two, because `mark_started` was never called."""
    run = _requested()
    assert run.version == 1

    run.mark_failed(TailoringFailureReason.NOT_QUEUED, _REQUESTED_AT)
    assert run.version == 2


# --- AC-5: revise_cv / revise_cover_letter are legal only from succeeded; the 3 x 2 illegal table --


@pytest.mark.parametrize(
    "from_status",
    [TailoringRunStatus.QUEUED, TailoringRunStatus.RUNNING, TailoringRunStatus.FAILED],
    ids=["queued", "running", "failed"],
)
def test_revise_cv_from_a_non_succeeded_status_raises_not_editable_and_leaves_the_run_unchanged(
    from_status: TailoringRunStatus,
) -> None:
    run = _run_not_editable(from_status)
    run.release_events()  # discard the setup events; only the failed revise attempt is under test
    version_before = run.version
    documents_before = run.documents

    with pytest.raises(TailoringRunNotEditable) as exc_info:
        run.revise_cv(_revised_cv(), expected_version=version_before, at=_EDITED_AT)

    assert exc_info.value.status is from_status
    assert run.status is from_status
    assert run.version == version_before
    assert run.documents == documents_before
    assert run.release_events() == ()


@pytest.mark.parametrize(
    "from_status",
    [TailoringRunStatus.QUEUED, TailoringRunStatus.RUNNING, TailoringRunStatus.FAILED],
    ids=["queued", "running", "failed"],
)
def test_revise_cover_letter_from_a_non_succeeded_status_raises_not_editable_and_leaves_the_run_unchanged(
    from_status: TailoringRunStatus,
) -> None:
    run = _run_not_editable(from_status)
    run.release_events()
    version_before = run.version
    documents_before = run.documents

    with pytest.raises(TailoringRunNotEditable) as exc_info:
        run.revise_cover_letter(_revised_letter(), expected_version=version_before, at=_EDITED_AT)

    assert exc_info.value.status is from_status
    assert run.status is from_status
    assert run.version == version_before
    assert run.documents == documents_before
    assert run.release_events() == ()


def test_revise_cv_on_a_queued_run_with_a_wrong_version_still_raises_not_editable() -> None:
    """Guard order (technical-plan.md's numbered steps): editability is checked **before** the
    version compare. A run that is both `queued` and given a version nothing could match must be
    told the more fundamental fact — that it is not editable at all — not sent off to re-fetch a
    version it could never have applied. If the guard order were reversed this would raise
    `TailoredDocumentVersionConflict` instead."""
    run = _requested()

    with pytest.raises(TailoringRunNotEditable):
        run.revise_cv(_revised_cv(), expected_version=999, at=_REQUESTED_AT)


def test_revise_cover_letter_on_a_running_run_with_a_wrong_version_still_raises_not_editable() -> (
    None
):
    run = _running()

    with pytest.raises(TailoringRunNotEditable):
        run.revise_cover_letter(_revised_letter(), expected_version=999, at=_STARTED_AT)


# --- AC-6: a stale expected_version raises TailoredDocumentVersionConflict carrying both numbers ---


def test_revise_cv_with_a_stale_expected_version_raises_conflict_and_leaves_the_run_unchanged() -> (
    None
):
    run = _succeeded()
    run.release_events()
    current_version = run.version  # 3
    stale = current_version - 1
    documents_before = run.documents

    with pytest.raises(TailoredDocumentVersionConflict) as exc_info:
        run.revise_cv(_revised_cv(), expected_version=stale, at=_EDITED_AT)

    assert exc_info.value.expected_version == stale
    assert exc_info.value.current_version == current_version
    assert run.version == current_version
    assert run.status is TailoringRunStatus.SUCCEEDED
    assert run.documents == documents_before
    assert run.release_events() == ()


def test_revise_cover_letter_with_a_stale_expected_version_raises_conflict_and_leaves_the_run_unchanged() -> (
    None
):
    run = _succeeded()
    run.release_events()
    current_version = run.version
    stale = (
        current_version + 1
    )  # ahead of the run, not just behind it — either mismatch is a conflict
    documents_before = run.documents

    with pytest.raises(TailoredDocumentVersionConflict) as exc_info:
        run.revise_cover_letter(_revised_letter(), expected_version=stale, at=_EDITED_AT)

    assert exc_info.value.expected_version == stale
    assert exc_info.value.current_version == current_version
    assert run.version == current_version
    assert run.documents == documents_before
    assert run.release_events() == ()


def test_revise_cv_with_the_matching_version_applies_bumps_and_records_the_event() -> None:
    run = _succeeded()
    run.release_events()
    current_version = run.version  # 3
    new_cv = _revised_cv()

    run.revise_cv(new_cv, expected_version=current_version, at=_EDITED_AT)

    assert run.version == current_version + 1
    current_documents = run.current_documents
    assert current_documents is not None
    assert current_documents.cv == new_cv

    events = run.release_events()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TailoredDocumentRevised)
    assert event.tailoring_run_id == _RUN_ID
    assert event.kind is TailoredDocumentKind.CV
    assert event.version == current_version + 1
    assert event.character_count == new_cv.character_count
    # The edited-at instant is not exposed by a public accessor of its own (only the mapping reads
    # `_cv_edited_at` directly); the event's `occurred_at` is what the aggregate records as "when",
    # and it must equal the instant the caller passed in.
    assert event.occurred_at == _EDITED_AT


def test_revise_cover_letter_with_the_matching_version_applies_bumps_and_records_the_event() -> (
    None
):
    run = _succeeded()
    run.release_events()
    current_version = run.version
    new_letter = _revised_letter()

    run.revise_cover_letter(new_letter, expected_version=current_version, at=_EDITED_AT)

    assert run.version == current_version + 1
    current_documents = run.current_documents
    assert current_documents is not None
    assert current_documents.cover_letter == new_letter

    events = run.release_events()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TailoredDocumentRevised)
    assert event.tailoring_run_id == _RUN_ID
    assert event.kind is TailoredDocumentKind.COVER_LETTER
    assert event.version == current_version + 1
    assert event.character_count == new_letter.character_count
    assert event.occurred_at == _EDITED_AT


def test_a_revision_with_content_identical_to_the_current_document_still_bumps_the_version() -> (
    None
):
    """The docstring's explicit sentence: "same text" is still a revision. Treating it as a no-op
    would make the version the client was shown lie about what it saw — the honest client never
    sends one (dirty tracking, AC-29), but the aggregate does not get to assume the caller is
    honest."""
    run = _succeeded()
    run.release_events()
    current_version = run.version
    same_content_cv = TailoredCv("a" * 400)  # equal in value to the draft written by mark_succeeded
    assert same_content_cv == run.documents.cv  # type: ignore[union-attr]  # precondition: truly identical

    run.revise_cv(same_content_cv, expected_version=current_version, at=_EDITED_AT)

    assert run.version == current_version + 1
    events = run.release_events()
    assert len(events) == 1
    assert events[0].occurred_at == _EDITED_AT


# --- TR-4 extended to the revision instant: at >= completed_at -------------------------------------


def test_revise_cv_earlier_than_completed_at_raises_invariant_violated_and_leaves_the_run_unchanged() -> (
    None
):
    run = _succeeded(completed_at=_COMPLETED_AT)
    run.release_events()
    version_before = run.version
    documents_before = run.documents
    earlier = _COMPLETED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        run.revise_cv(_revised_cv(), expected_version=version_before, at=earlier)

    assert run.version == version_before
    assert run.documents == documents_before
    assert run.release_events() == ()


def test_revise_cv_at_exactly_completed_at_is_legal() -> None:
    """TR-4 is `>=`, not `>` — a revision made in the same whole second the run completed must not be
    punished for being fast, the same rule `mark_started`/`mark_succeeded` already hold for their own
    instants."""
    run = _succeeded(completed_at=_COMPLETED_AT)
    version_before = run.version

    run.revise_cv(_revised_cv(), expected_version=version_before, at=_COMPLETED_AT)

    assert run.version == version_before + 1


def test_revise_cover_letter_earlier_than_completed_at_raises_invariant_violated() -> None:
    run = _succeeded(completed_at=_COMPLETED_AT)
    earlier = _COMPLETED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        run.revise_cover_letter(_revised_letter(), expected_version=run.version, at=earlier)


def test_revise_cover_letter_at_exactly_completed_at_is_legal() -> None:
    run = _succeeded(completed_at=_COMPLETED_AT)
    version_before = run.version

    run.revise_cover_letter(_revised_letter(), expected_version=version_before, at=_COMPLETED_AT)

    assert run.version == version_before + 1


# --- TR-11: documents (the draft) is never overwritten by a revision --------------------------------


def test_documents_is_unchanged_after_revising_both() -> None:
    """`documents` keeps meaning "what the model produced" — the same value the run held right after
    `mark_succeeded` — even after both of its documents have been replaced in `current_documents`."""
    run = _succeeded()
    draft = run.documents
    current_version = run.version

    run.revise_cv(_revised_cv(), expected_version=current_version, at=_EDITED_AT)
    run.revise_cover_letter(_revised_letter(), expected_version=run.version, at=_EDITED_AT)

    assert run.documents == draft


# --- current_documents: None before succeeded; draft-only; one revision; both --------------------


@pytest.mark.parametrize(
    "status",
    [TailoringRunStatus.QUEUED, TailoringRunStatus.RUNNING, TailoringRunStatus.FAILED],
    ids=["queued", "running", "failed"],
)
def test_current_documents_is_none_before_the_run_has_succeeded(
    status: TailoringRunStatus,
) -> None:
    run = _run_not_editable(status)

    assert run.current_documents is None


def test_current_documents_is_the_draft_pair_when_nothing_has_been_revised() -> None:
    run = _succeeded()

    assert run.current_documents == run.documents


def test_current_documents_reflects_a_revised_cv_but_keeps_the_draft_cover_letter() -> None:
    run = _succeeded()
    draft = run.documents
    assert draft is not None
    new_cv = _revised_cv()

    run.revise_cv(new_cv, expected_version=run.version, at=_EDITED_AT)

    current = run.current_documents
    assert current is not None
    assert current.cv == new_cv
    assert current.cover_letter == draft.cover_letter


def test_current_documents_reflects_a_revised_cover_letter_but_keeps_the_draft_cv() -> None:
    run = _succeeded()
    draft = run.documents
    assert draft is not None
    new_letter = _revised_letter()

    run.revise_cover_letter(new_letter, expected_version=run.version, at=_EDITED_AT)

    current = run.current_documents
    assert current is not None
    assert current.cv == draft.cv
    assert current.cover_letter == new_letter


def test_current_documents_reflects_both_revisions_once_both_have_been_made() -> None:
    run = _succeeded()
    new_cv = _revised_cv()
    new_letter = _revised_letter()

    run.revise_cv(new_cv, expected_version=run.version, at=_EDITED_AT)
    run.revise_cover_letter(new_letter, expected_version=run.version, at=_EDITED_AT)

    current = run.current_documents
    assert current is not None
    assert current.cv == new_cv
    assert current.cover_letter == new_letter


# --- 1.3's AC-5 extended: the mapped-attribute constructor hole, for the five new private names ----
#
# Green on arrival, exactly like their 1.3 counterparts in test_tailoring_run.py: `__init__` takes no
# arguments today, so Python's own signature check refuses both calls before any body runs. They are
# recorded here so the hole `registry.map_imperatively` opens at T19 (mapping) stays closed for the
# attributes this slice adds, not only the sixteen 1.3 already guarded.


def test_tailoring_run_cannot_be_constructed_via_the_version_mapped_attribute_name() -> None:
    with pytest.raises(TypeError):
        TailoringRun(_version=1)  # type: ignore[call-arg]


def test_tailoring_run_cannot_be_constructed_via_the_edited_cv_mapped_attribute_name() -> None:
    with pytest.raises(TypeError):
        TailoringRun(_edited_cv=_revised_cv())  # type: ignore[call-arg]
