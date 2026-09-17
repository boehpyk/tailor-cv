"""Application tests for `RenderDocumentInline` (T6, RED).

**Why fakes, not a real Postgres:** the same reason `test_request_export.py`'s module docstring
gives. This use case is tested against the ports it actually depends on — `DocumentRendererPort`
(`FakeDocumentRenderer`) and, through the *real* `GetTailoringRunForSession`, `TailoringRunRepository`
(`FakeTailoringRunRepository`) and `GuestSessionRepository` (`FakeGuestSessionRepository`).

**Why the `DocumentRenderFailed` propagation test matters more than any other in this file.**
`RenderDocumentInline` is the deliberate *opposite* of `RenderExportJob`: there is no row here and
nothing was spent, so the identical exception family must escape to the router rather than being
caught and recorded (ADR-0014 §2, X-5/X-6). A reader arriving from `render_export_job.py`, which
*does* catch it, may "fix" this module to match. The test below therefore asserts the **propagation**
explicitly — wrapping the call in `pytest.raises(DocumentRenderFailed)` — so that "fixing" it into a
caught-and-recorded failure turns this test red rather than silently changing behaviour underneath a
test that only checked "did not raise".

Every assertion below states what `RenderDocumentInline.__call__` **should** do per technical-plan.md's
"Application layer" §3 ("Flow") and feature-spec.md's failure contract rows X-1 … X-8, never what the
(currently `NotImplementedError`) code was observed doing. Because the skeleton's `__call__` body is
an unconditional `raise NotImplementedError`, every test below is expected to fail on that line: a
real red, not a vacuous pass, and not an `ImportError`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.export.render_document_inline import (
    RenderDocumentInline,
    RenderDocumentInlineCommand,
    RenderedInlineDocument,
)
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRunForSession
from tailorcraft.domain.export.errors import (
    DocumentRenderError,
    ExportFormatNotInline,
    TailoringRunNotExportable,
)
from tailorcraft.domain.export.events import DocumentRenderedInline
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound, TailoringRunNotOwnedBySession
from tailorcraft.domain.tailoring.value_objects import (
    TailoredCv,
    TailoredDocumentKind,
    TailoringRunId,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.export.support import a_documents, queued_run, succeeded_run
from tests.integration.fakes import (
    FakeDocumentRenderer,
    FakeGuestSessionRepository,
    FakeTailoringRunRepository,
    RecordingEventPublisher,
    create_active_session,
)


def _use_case(
    runs: FakeTailoringRunRepository,
    sessions: FakeGuestSessionRepository,
    renderer: FakeDocumentRenderer,
    events: RecordingEventPublisher,
    clock: FixedClock,
) -> RenderDocumentInline:
    get_tailoring_run = GetTailoringRunForSession(runs, sessions, clock)
    return RenderDocumentInline(get_tailoring_run, renderer, events, clock)


# --- Happy path, both document kinds ------------------------------------------------------------


async def test_renders_the_cv_to_markdown_and_publishes_document_rendered_inline(
    clock: FixedClock,
) -> None:
    events = RecordingEventPublisher()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    data = a_documents().cv.value.encode("utf-8")
    renderer = FakeDocumentRenderer(data)
    use_case = _use_case(runs, sessions, renderer, events, clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.MD,
    )

    result = await use_case(cmd)

    assert result == RenderedInlineDocument(
        data=data, format=ExportFormat.MD, document=TailoredDocumentKind.CV
    )
    assert renderer.calls == [(a_documents().cv.value, TailoredDocumentKind.CV, ExportFormat.MD)]

    published = [e for e in events.published if isinstance(e, DocumentRenderedInline)]
    assert len(published) == 1
    assert published[0].tailoring_run_id == run.id
    assert published[0].document is TailoredDocumentKind.CV
    assert published[0].format is ExportFormat.MD
    assert published[0].byte_size == len(data)


async def test_renders_the_cover_letter_to_plain_text(clock: FixedClock) -> None:
    events = RecordingEventPublisher()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    data = a_documents().cover_letter.value.encode("utf-8")
    renderer = FakeDocumentRenderer(data)
    use_case = _use_case(runs, sessions, renderer, events, clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.COVER_LETTER,
        format=ExportFormat.TXT,
    )

    result = await use_case(cmd)

    assert result.data == data
    assert result.format is ExportFormat.TXT
    assert result.document is TailoredDocumentKind.COVER_LETTER
    assert renderer.calls == [
        (a_documents().cover_letter.value, TailoredDocumentKind.COVER_LETTER, ExportFormat.TXT)
    ]


# --- X-2 / X-3: session and run resolution, inherited whole from GetTailoringRunForSession ----


async def test_expired_guest_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock, ttl_hours=1)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    renderer = FakeDocumentRenderer(b"unused")
    stale_clock = FixedClock(clock.now() + timedelta(hours=2))
    use_case = _use_case(runs, sessions, renderer, events, stale_clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.MD,
    )

    with pytest.raises(GuestSessionExpired):
        await use_case(cmd)

    assert renderer.calls == []


async def test_unknown_guest_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    runs = FakeTailoringRunRepository()
    events = RecordingEventPublisher()
    renderer = FakeDocumentRenderer(b"unused")
    use_case = _use_case(runs, sessions, renderer, events, clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=GuestSessionId(value=uuid4()),
        tailoring_run_id=TailoringRunId(value=uuid4()),
        document=TailoredDocumentKind.CV,
        format=ExportFormat.MD,
    )

    with pytest.raises(GuestSessionNotFound):
        await use_case(cmd)

    assert renderer.calls == []


async def test_run_owned_by_a_different_session_raises_tailoring_run_not_found_chained_from_not_owned(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    owner = await create_active_session(sessions, clock, token_hash="a" * 64)
    stranger = await create_active_session(sessions, clock, token_hash="b" * 64)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=owner.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    renderer = FakeDocumentRenderer(b"unused")
    use_case = _use_case(runs, sessions, renderer, events, clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=stranger.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.MD,
    )

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await use_case(cmd)

    assert isinstance(exc_info.value.__cause__, TailoringRunNotOwnedBySession)
    assert renderer.calls == []


# --- X-4: the run is not `succeeded` -------------------------------------------------------------


async def test_queued_run_raises_tailoring_run_not_exportable(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = queued_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    renderer = FakeDocumentRenderer(b"unused")
    use_case = _use_case(runs, sessions, renderer, events, clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.MD,
    )

    with pytest.raises(TailoringRunNotExportable) as exc_info:
        await use_case(cmd)

    assert exc_info.value.status is run.status
    assert renderer.calls == []


# --- X-1: a queued format reaches this use case ----------------------------------------------


async def test_queued_format_raises_export_format_not_inline(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    renderer = FakeDocumentRenderer(b"unused")
    use_case = _use_case(runs, sessions, renderer, events, clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    with pytest.raises(ExportFormatNotInline) as exc_info:
        await use_case(cmd)

    assert exc_info.value.format is ExportFormat.PDF
    assert renderer.calls == []


# --- X-8: `current_documents` is the revision when one exists, else the draft ------------------


async def test_reads_the_revision_when_one_exists_rather_than_the_original_draft(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    revised_cv = TailoredCv("r" * 450)
    run.revise_cv(revised_cv, expected_version=run.version, at=clock.now())
    await runs.add(run)
    events = RecordingEventPublisher()
    renderer = FakeDocumentRenderer(revised_cv.value.encode("utf-8"))
    use_case = _use_case(runs, sessions, renderer, events, clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.MD,
    )

    await use_case(cmd)

    assert renderer.calls == [(revised_cv.value, TailoredDocumentKind.CV, ExportFormat.MD)]
    assert renderer.calls[0][0] != a_documents().cv.value


async def test_reads_the_original_draft_when_no_revision_exists(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    renderer = FakeDocumentRenderer(a_documents().cover_letter.value.encode("utf-8"))
    use_case = _use_case(runs, sessions, renderer, events, clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.COVER_LETTER,
        format=ExportFormat.TXT,
    )

    await use_case(cmd)

    assert renderer.calls == [
        (a_documents().cover_letter.value, TailoredDocumentKind.COVER_LETTER, ExportFormat.TXT)
    ]


# --- The propagation contrast with RenderExportJob ---------------------------------------------


async def test_document_render_failed_propagates_rather_than_being_recorded(
    clock: FixedClock,
) -> None:
    """`DocumentRenderFailed` must **escape** this use case — the deliberate opposite of
    `RenderExportJob`, which catches the identical exception family and records it on the job
    (ADR-0014 §2). There is no row here and nothing was spent, so there is nothing to record the
    failure *on*. If a future change caught this exception and returned some "failed" result
    instead of letting it propagate, this test — unlike a bare "does not raise" check — goes red."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    failure = DocumentRenderError()
    renderer = FakeDocumentRenderer(failure)
    use_case = _use_case(runs, sessions, renderer, events, clock)
    cmd = RenderDocumentInlineCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.MD,
    )

    with pytest.raises(DocumentRenderError) as exc_info:
        await use_case(cmd)

    assert exc_info.value is failure
    # Nothing is published on the failing path: the event is the success line for a path with no
    # row, and this call never succeeded.
    assert events.published == []
