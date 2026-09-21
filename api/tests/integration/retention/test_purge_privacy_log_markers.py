"""AC-38's planted-marker privacy test (`/verify` MAJOR 2): "nothing this slice logs can identify a
person."

Before this file, `grep -rn "AC-38" api/tests/` returned nothing, and the spec's Privacy section
said the "never logged" list is *"asserted by AC-38's planted-marker test, not by intention"* — a
sentence that was false. The one `caplog` test in the slice (`test_purge_cli.py`'s
`test_purge_completed_log_line_is_emitted_on_an_empty_backlog_with_its_full_field_set`) is a
**positive** field-set assertion over an *empty* backlog: it proves the eight counts are present, and
because the backlog is empty there is no CV, no filename, no key and no path anywhere in the run for
it to have missed.

This file drives the opposite backlog: one session that owns a base CV, a job posting, a succeeded
**and revised** tailoring run, and a ready export job — every text field and every identifying field
the domain has, each carrying its own distinctive planted marker — plus, on the same volume, a
genuine orphan (a `.part` file and an unreferenced final file). It runs a **real** `purge-guests` and
a **real** `purge-guests --orphans` over that fixture, through the exact entry points production uses
(`purge_command._purge_guests` / `_reclaim_orphans` — the same composition roots `run_from_cli`
calls), captures every log record with `caplog`, and greps the captured text for every marker.

**Why a real filesystem, not the fake `FileStorePort` other application tests use** (the `.part`
lesson, T18b). A fake has no filesystem, so it has no keys and no joined paths in the first place —
there is nothing for it to leak, and a test built on one could pass by having nothing to prove. Only a
*real* adapter genuinely holds a storage key and a resolved filesystem path in memory across the run
this file drives (`LocalFileStore`/`LocalOrphanFileScanner`'s own code paths), so only a real adapter
can stand as evidence that neither ever reaches a log record. This file's own happy path never
reaches an *error* branch inside either adapter — that claim belongs to the R-4 tests in
`test_purge_cli.py` and `test_retention_task_purge_run.py`, which plant a refusal there and assert on
`caplog` directly. `tmp_path` is handed to `purge-guests` through
`settings.model_copy(update={"upload_dir": tmp_path})` — the same technique `test_purge_cli.py` uses
to swap `database_url` — so this run touches a private, function-scoped tree rather than the
session-scoped `settings.upload_dir` every other test file shares, and cannot race another test's
files or be raced by one.

**What this file does NOT re-prove.** `test_purge_database.py`'s
`test_the_purge_never_selects_a_pii_column_or_touches_another_contexts_table` already proves, by
capturing the actual SQL, that the purge's own `SELECT`s never name a text column — that is a
structural guarantee about the **read side** and does not need a second proof here. What that test
cannot see is the **write side**: a storage key and a resolved filesystem path *are* genuinely held in
memory by this run (`ExpiringGuestSession.files`, `FileRef`, `LocalFileStore._resolve_contained`) and
passed to `FileStorePort.delete` / `delete_partial` — this file is the one that proves neither ever
reaches a log record.

**One deliberate, named exception, per the spec's own comment at the line.**
`infrastructure/files/orphan_scanner.py:151` logs `root=str(self._root)` on `retention.
orphan_scan_failed` — the *configured volume root*, not a guest-derived path, and that line only
fires on an `OSError` reading the root itself. This run's happy path never reaches it, and the
assertions below are scoped to the **joined** path (`root / key`, which carries a UUID) rather than
the bare root, so this test would not flag that line even if it did fire.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    CvContentType,
    ExtractedText,
    OriginalFilename,
)
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocumentKind,
    TailoredDocuments,
    TailoringRunId,
)
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.retention import purge_command
from tailorcraft.infrastructure.settings import Settings

# The four repository imports are deferred into the test function body rather than hoisted here —
# see `test_posting.py`'s identical note at its own deferred repository import. Every mapping
# module's `map_imperatively()` call runs at ITS OWN import time, and conftest.py's `_mappings`
# fixture (session-scoped, autouse) only runs at fixture setup — *after* every test module has
# already been collected and imported. A repository module such as `repositories/export/export_job.py`
# reads `ExportJob._id` as a plain class attribute at ITS OWN import time to build its
# `InstrumentedAttribute` casts, and that attribute exists only once some mapping module has already
# run — which, at collection time, none has. Importing the four repositories at module scope here
# reproduced exactly that `AttributeError` at collection.

# --- Safety: the 1.4 guard, reproduced here rather than imported (`test_purge_database.py` and
# `test_purge_cli.py` each keep their own copy too, for the same stated reason). ------------------


def _assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        "refusing to run a deleting privacy test against a URL that is not the test database: "
        f"{settings.database_url!r}"
    )


# --- Markers ---------------------------------------------------------------------------------------

_MARKER_PREFIX = "QA38MARKER"


def _marker(label: str) -> str:
    """A distinctive, grep-able substring — unique per call, so a leak of one field cannot be
    mistaken for a leak of another."""
    return f"{_MARKER_PREFIX}-{label}-{uuid4().hex}"


def _fixed_length_marker(label: str, length: int) -> str:
    """A marker padded/truncated to an exact length, for the one field with a fixed-width column
    (`token_hash`, `CHAR(64)`). The distinctive prefix survives truncation as long as `length` is
    comfortably above its own length, which it is here (64 against ~30)."""
    raw = _marker(label)
    return (raw + "x" * length)[:length]


def _padded_document(marker: str, *, filler_repeats: int) -> str:
    """`marker`, plus enough filler prose to clear whichever value object's non-whitespace floor is
    in play — `TailoredCv` needs 400, `CoverLetter`/`ExtractedText` need 200, `JobPostingText` needs
    100. The filler is ordinary prose (no digits, no markup) so it cannot itself be mistaken for a
    marker, and the marker sits on its own line so no floor's whitespace handling could ever fuse it
    into the filler and make it ungreppable.
    """
    filler = "This sentence exists only to satisfy a minimum character floor. "
    return f"{marker}\n" + (filler * filler_repeats)


def _a_file_ref() -> FileRef:
    """A syntactically valid `FileRef` with nothing behind it yet — the same construction the other
    two retention test files use for a genuine orphan."""
    generated = uuid4()
    hex_digits = generated.hex
    return FileRef(key=f"{hex_digits[0:2]}/{hex_digits[2:4]}/{generated}.pdf")


def _write_part_file(root: Path, ref: FileRef, *, at: datetime) -> Path:
    part_path = root / f"{ref.key}.part"
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(b"an interrupted write, never fsynced to its final name")
    _age(part_path, at)
    return part_path


def _age(path: Path, at: datetime) -> None:
    timestamp = at.timestamp()
    os.utime(path, (timestamp, timestamp))


def _a_past_instant(hours_ago: float) -> datetime:
    """A real, whole-second, timezone-aware wall-clock instant — `purge_command`'s own entry points
    build a `SystemClock()` internally, never the test's `FixedClock`, so a session must be expired
    against **real** time for the purge under test to select it (`test_purge_cli.py`'s own helper of
    the same name and the same reason)."""
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).replace(microsecond=0)


# --- Fixtures ----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    """`_purge_guests`/`_reclaim_orphans` are called directly, never through `run_from_cli`, so
    nothing else in this module configures structlog — without this, `caplog` would only capture
    output if some earlier test in the session happened to configure logging first
    (`test_purge_cli.py`'s identical fixture, same reason)."""
    configure_logging(settings)


@pytest_asyncio.fixture
async def committed_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session bound directly to the session-scoped `engine`, never the rolled-back `connection`/
    `session` fixtures: `purge_command`'s own composition roots build a fresh engine and read this
    data back from a genuinely committed transaction, so the fixture data must be genuinely committed
    too (`test_purge_database.py`'s `committing_session`, minus the pinned-connection `SET` machinery
    that file needs and this one does not — nothing here issues a session-level `SET`).
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session


# --- The planted fixture -------------------------------------------------------------------------


async def test_a_full_purge_and_orphan_sweep_never_log_any_planted_marker(
    settings: Settings,
    engine: AsyncEngine,
    committed_session: AsyncSession,
    tmp_path: Path,
    clear_redis: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-38. One guest session owning every kind of PII this product holds, each field carrying its
    own marker; a genuine orphan and a genuine `.part` file beside it on the same volume; a real
    `purge-guests` and a real `purge-guests --orphans`, both captured by `caplog`; then every marker,
    every storage key and every joined filesystem path is asserted absent from the captured text.

    Two guard assertions at the end make sure this is not vacuous: the completion lines for both
    runs **are** present (so `caplog` genuinely captured this run's output and was not simply
    misconfigured), and the exit codes are the success ones (so a run that silently failed before
    touching any of the planted data cannot pass this test by having nothing to leak).
    """
    # Deferred imports — see the module-level comment beside where these four would otherwise sit.
    from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
        SqlAlchemyExportJobRepository,
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

    _assert_test_database(settings)
    local_settings = settings.model_copy(update={"upload_dir": tmp_path})
    files = LocalFileStore(tmp_path)

    now = datetime.now(UTC).replace(microsecond=0)
    requested_at = now - timedelta(hours=3)
    uploaded_at = now - timedelta(hours=3)
    extracted_at = now - timedelta(hours=2, minutes=58)
    posting_created_at = now - timedelta(hours=3)
    started_at = now - timedelta(hours=2, minutes=55)
    completed_at = now - timedelta(hours=2, minutes=50)
    revised_at = now - timedelta(hours=2, minutes=45)

    session_id = GuestSessionId(uuid4())
    token_hash_marker = _fixed_length_marker("TOKENHASH", 64)
    await committed_session.execute(
        guest_session_table.insert().values(
            id=session_id,
            token_hash=token_hash_marker,
            created_at=now - timedelta(hours=25),
            expires_at=now - timedelta(hours=1),  # expired: the predicate that selects it
        )
    )
    await committed_session.commit()

    # --- The base CV: a marker filename, marker extracted text, and marker bytes on disk ---------
    cvs = SqlAlchemyBaseCvRepository(committed_session)
    cv_id = cvs.next_identity()
    cv_ref = FileRef.for_base_cv(cv_id, CvContentType.PDF)
    filename_marker = _marker("FILENAME")
    extracted_text_marker = _marker("EXTRACTEDTEXT")
    cv_bytes_marker = _marker("CVBYTES")
    cv = BaseCv.upload(
        id=cv_id,
        guest_session_id=session_id,
        original_filename=OriginalFilename(f"{filename_marker}.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=64,
        file=cv_ref,
        uploaded_at=uploaded_at,
    )
    cv.mark_extracted(
        ExtractedText(_padded_document(extracted_text_marker, filler_repeats=10)), extracted_at
    )
    await cvs.add(cv)
    await committed_session.commit()
    await files.put(cv_ref, f"{cv_bytes_marker}\nthe raw file bytes on disk".encode())

    # --- The job posting: marker posting text ------------------------------------------------------
    postings = SqlAlchemyJobPostingRepository(committed_session)
    posting_text_marker = _marker("POSTINGTEXT")
    posting = JobPosting.from_pasted_text(
        id=postings.next_identity(),
        guest_session_id=session_id,
        text=JobPostingText(_padded_document(posting_text_marker, filler_repeats=5)),
        created_at=posting_created_at,
    )
    await postings.add(posting)
    await committed_session.commit()

    # --- The tailoring run: marker tailored CV, marker cover letter, then a marker REVISION -------
    runs = SqlAlchemyTailoringRunRepository(committed_session)
    tailored_cv_marker = _marker("TAILOREDCV")
    cover_letter_marker = _marker("COVERLETTER")
    revision_marker = _marker("REVISIONTEXT")
    run = TailoringRun.request(
        id=runs.next_identity(),
        guest_session_id=session_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=requested_at,
    )
    run.mark_started(started_at)
    run.mark_succeeded(
        TailoredDocuments(
            cv=TailoredCv(_padded_document(tailored_cv_marker, filler_repeats=20)),
            cover_letter=CoverLetter(_padded_document(cover_letter_marker, filler_repeats=10)),
        ),
        LlmCallMetrics(
            model=ModelName("gemini-2.5-flash"),
            prompt_version=PromptVersion("v1"),
            prompt_tokens=100,
            completion_tokens=50,
            duration_ms=500,
        ),
        completed_at,
    )
    await runs.add(run)
    await committed_session.commit()

    run.revise_cv(
        TailoredCv(_padded_document(revision_marker, filler_repeats=20)),
        expected_version=run.version,
        at=revised_at,
    )
    await runs.save(run)
    await committed_session.commit()

    # --- A ready export job: a second, independent storage key and path --------------------------
    jobs = SqlAlchemyExportJobRepository(committed_session)
    export_bytes_marker = _marker("EXPORTBYTES")
    job = ExportJob.request(
        id=jobs.next_identity(),
        guest_session_id=session_id,
        tailoring_run_id=TailoringRunId(value=uuid4()),
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=1,
        requested_at=requested_at,
    )
    job.mark_started(started_at)
    job.mark_ready(byte_size=64, render_duration_ms=50, at=completed_at)
    await jobs.add(job)
    await committed_session.commit()
    export_ref = job.storage_ref
    await files.put(export_ref, f"{export_bytes_marker}\nthe rendered export bytes".encode())

    # --- A genuine orphan and a genuine `.part`, aged past the 48h floor (24h window + 24h grace) --
    orphan_ref = _a_file_ref()
    await files.put(orphan_ref, b"nobody references this any more")
    _age(tmp_path / orphan_ref.key, now - timedelta(hours=60))
    part_ref = _a_file_ref()
    _write_part_file(tmp_path, part_ref, at=now - timedelta(hours=60))

    try:
        # --- The run: a real purge, then a real orphan sweep, both captured -----------------------
        with caplog.at_level(logging.DEBUG):
            purge_exit_code = await purge_command._purge_guests(
                local_settings, dry_run=False, limit=None
            )
            orphan_exit_code = await purge_command._reclaim_orphans(
                local_settings, dry_run=False, limit=None, grace_hours=24
            )

        # --- Guard against vacuity: this run must have actually happened and actually logged -------
        assert purge_exit_code == purge_command.EXIT_OK, caplog.text
        assert orphan_exit_code == purge_command.EXIT_OK, caplog.text
        assert "retention.purge_completed" in caplog.text
        assert "retention.orphan_scan_completed" in caplog.text

        # --- The actual claim: not one marker, key or joined path made it into a log record --------
        forbidden: dict[str, str] = {
            "the raw CV file's marker filename": filename_marker,
            "the extracted text": extracted_text_marker,
            "the raw CV bytes on disk": cv_bytes_marker,
            "the job posting text": posting_text_marker,
            "the tailored CV": tailored_cv_marker,
            "the cover letter": cover_letter_marker,
            "the user's revision": revision_marker,
            "the export's rendered bytes": export_bytes_marker,
            "the guest session's token hash": token_hash_marker,
            "the base CV's storage key": cv_ref.key,
            "the base CV's joined filesystem path": str(tmp_path / cv_ref.key),
            "the export job's storage key": export_ref.key,
            "the export job's joined filesystem path": str(tmp_path / export_ref.key),
            "the orphan's storage key": orphan_ref.key,
            "the orphan's joined filesystem path": str(tmp_path / orphan_ref.key),
            "the .part file's storage key": f"{part_ref.key}.part",
            "the .part file's joined filesystem path": str(tmp_path / f"{part_ref.key}.part"),
        }
        for description, marker in forbidden.items():
            assert marker not in caplog.text, (
                f"{description} ({marker!r}) appeared in a captured log record — "
                f"Constitution §8 / AC-38. Full captured text:\n{caplog.text}"
            )
    finally:
        await committed_session.execute(
            guest_session_table.delete().where(guest_session_table.c.id == session_id)
        )
        await committed_session.commit()
