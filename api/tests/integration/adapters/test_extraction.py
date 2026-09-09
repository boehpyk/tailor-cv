"""Adapter tests for `PypdfDocxTextExtractor` (`CvTextExtractorPort`), written **after** (T29):
real extraction from the happy-path corpus, the failure-contract translation for the known-bad
fixtures, and the 50-page cap behind the `slow` marker (technical-plan.md's Adapters row; F-6…F-11).

**OQ-9** — the 200-non-whitespace-character floor on `ExtractedText` is documented as provisional,
"chosen, not measured" (technical-plan.md, `domain/intake/value_objects.py`). `test_oq9_*` below
measures what the committed corpus actually produces and asserts *properties* against the floor
(comfortably above it / comfortably below it) rather than an exact character count copied from a
run of the code — CLAUDE.md's rule that a test must encode what the code should do, not what it was
observed doing, applies especially hard to a test investigating a number the code itself calls
unmeasured. The measured numbers are reported in this module's own docstring rather than silently
adjusting the floor: `sample.{pdf,docx,txt}` (a realistic one-page CV body) extract to 867
non-whitespace characters — more than 4x the 200 floor — and `tiny.txt` (chosen to be "too short")
extracts to 39, comfortably below it. Nothing in the corpus contradicts 200 as a floor: it does not
falsely reject the realistic sample, and it does correctly reject the deliberately-too-short one. The
corpus does not, however, contain a fixture *near* the boundary (150-250 non-whitespace characters),
so this run cannot confirm 200 is well-calibrated at the edge — only that it is not obviously wrong
for the cases the corpus covers. **Recommendation: keep 200 unchanged** on the evidence available;
a boundary-straddling fixture would be a good future addition to the corpus (README.md already
invites exactly that kind of growth) if OQ-9 needs a tighter answer later.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import time
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.errors import DependencyError

from tailorcraft.domain.intake.errors import (
    CorruptCvFile,
    CvExtractionFailed,
    CvHasNoTextLayer,
    CvHasTooManyPages,
    CvTextTooShort,
    EncryptedCvFile,
)
from tailorcraft.domain.intake.value_objects import (
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
)
from tailorcraft.infrastructure.intake.extraction import PypdfDocxTextExtractor
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "cvs"

# `sample.{pdf,docx,txt}` share this body (tests/fixtures/cvs/README.md): known phrases the
# extracted text must contain. Asserting a *property* the fixture's author put there on purpose,
# rather than an exact string copied from a run of the extractor.
_KNOWN_NAME = "Alex Rivera"
_KNOWN_EMPLOYER = "Northwind Logistics"


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _extractor() -> PypdfDocxTextExtractor:
    return PypdfDocxTextExtractor(timeout_seconds=10, max_pages=50)


# --- Happy path: real text out of the corpus ------------------------------------------------------

_HAPPY_PATH_CASES = [
    pytest.param("sample.pdf", CvContentType.PDF, id="sample.pdf"),
    pytest.param("sample.docx", CvContentType.DOCX, id="sample.docx"),
    pytest.param("sample.txt", CvContentType.TXT, id="sample.txt"),
]


@pytest.mark.parametrize(("fixture_name", "content_type"), _HAPPY_PATH_CASES)
async def test_extracts_real_text_containing_known_phrases(
    fixture_name: str, content_type: CvContentType
) -> None:
    data = _read_fixture(fixture_name)

    result = await _extractor().extract(content_type, data)

    assert isinstance(result, ExtractedText)
    assert result.character_count >= 200
    assert _KNOWN_NAME in result.value
    assert _KNOWN_EMPLOYER in result.value


# --- Failure-contract translation for the known-bad fixtures ---------------------------------------

_FAILURE_CASES = [
    pytest.param("encrypted.pdf", CvContentType.PDF, EncryptedCvFile, id="F-7-encrypted"),
    pytest.param("corrupt.pdf", CvContentType.PDF, CorruptCvFile, id="F-8-corrupt"),
    pytest.param("scanned.pdf", CvContentType.PDF, CvHasNoTextLayer, id="F-9-no_text_layer"),
    pytest.param("tiny.txt", CvContentType.TXT, CvTextTooShort, id="F-10-too_short"),
]


@pytest.mark.parametrize(("fixture_name", "content_type", "expected_exc"), _FAILURE_CASES)
async def test_translates_known_bad_fixtures_to_the_matching_failure(
    fixture_name: str, content_type: CvContentType, expected_exc: type[CvExtractionFailed]
) -> None:
    data = _read_fixture(fixture_name)

    with pytest.raises(expected_exc):
        await _extractor().extract(content_type, data)


# --- The 50-page cap, checked before any page's content is parsed ----------------------------------


@pytest.mark.slow
async def test_a_pdf_over_the_page_cap_is_refused_before_any_page_is_parsed() -> None:
    writer = PdfWriter()
    for _ in range(51):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(CvHasTooManyPages):
        await _extractor().extract(CvContentType.PDF, buffer.getvalue())


# --- OQ-9: the 200-character floor, measured against the corpus ------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "content_type"),
    [
        pytest.param("sample.pdf", CvContentType.PDF, id="sample.pdf"),
        pytest.param("sample.docx", CvContentType.DOCX, id="sample.docx"),
        pytest.param("sample.txt", CvContentType.TXT, id="sample.txt"),
    ],
)
async def test_oq9_a_realistic_one_page_cv_clears_the_200_character_floor_by_a_wide_margin(
    fixture_name: str, content_type: CvContentType
) -> None:
    """A property assertion, not the exact copied count (module docstring): a realistic one-page CV
    body must land well clear of the floor, not just barely over it — if it did not, 200 would be
    too aggressive for ordinary content and this test would be the place that says so."""
    data = _read_fixture(fixture_name)

    result = await _extractor().extract(content_type, data)

    # more than 4x the floor on the measured corpus (867 non-whitespace characters) — a wide margin
    # a small future change to `sample.*`'s body should not accidentally erode to nothing.
    assert result.character_count >= 400


# -----------------------------------------------------------------------------------------------
# The `except Exception` catch-all in `extract()` (T-after-fix, api-dev's two CRITICAL findings):
# every unexpected library exception becomes `CvExtractionFailed(EXTRACTOR_ERROR)`, never escapes.
# -----------------------------------------------------------------------------------------------


def _corrupt_bytes(data: bytes, seed: int, n: int) -> bytes:
    """Flip `n` random bytes of `data` under a fixed `seed` — deterministic across runs and
    machines, which is what lets a specific finding below (`seed=12440`, the AC-12 guard-proof
    test) be reproduced byte-for-byte rather than merely "some corrupted variant or other"."""
    rng = random.Random(seed)  # noqa: S311 — deterministic corruption, not cryptography
    mutable = bytearray(data)
    for position in rng.sample(range(len(mutable)), n):
        mutable[position] = rng.randrange(256)
    return bytes(mutable)


# `n=5` on `sample.pdf` (2067 bytes) under seeds 12345..12644 was measured, before this fix, to
# escape `extract()` as a bare `KeyError` or `AttributeError` on 10 of these 300 variants — the
# same order of magnitude as api-dev's reported 21/300 on a different corruption profile, and the
# same two exception types. 300 keeps this adapter test well under a second (measured: ~150 ms
# including 300 real `PdfReader` parses) while still being enough variants to reliably reproduce
# the escape when the catch-all is removed — see the mutation-testing note in this module's own
# history for the exact command used to confirm that.
_SWEEP_SEED_BASE = 12345
_SWEEP_COUNT = 300
_SWEEP_BYTES_FLIPPED = 5


async def test_corruption_sweep_never_escapes_extract_as_anything_but_cv_extraction_failed() -> (
    None
):
    """The highest-value regression test for the catch-all (CLAUDE.md's "guard the class of bug,
    not one instance of it"): 300 independently-corrupted variants of `sample.pdf`, run through the
    real `extract()`, must each either succeed or raise a `CvExtractionFailed` subclass — never
    anything else. Before `extract()` grew its `except Exception` clause, this exact sweep produced
    `KeyError` and `AttributeError` escapes (verified empirically while writing this test); a single
    hand-picked corrupt fixture would never have caught either, because neither happens to be the
    one failure mode `corrupt.pdf` (a truncated file) exercises.
    """
    extractor = _extractor()
    data = _read_fixture("sample.pdf")
    unexpected: list[tuple[int, str, str]] = []

    for i in range(_SWEEP_COUNT):
        variant = _corrupt_bytes(data, seed=_SWEEP_SEED_BASE + i, n=_SWEEP_BYTES_FLIPPED)
        try:
            await extractor.extract(CvContentType.PDF, variant)
        except CvExtractionFailed:
            pass
        except Exception as exc:
            unexpected.append((i, type(exc).__name__, str(exc)[:120]))

    assert not unexpected, (
        f"{len(unexpected)}/{_SWEEP_COUNT} corrupted variants escaped extract() as something "
        f"other than CvExtractionFailed: {unexpected[:5]}"
    )


async def test_a_docx_with_a_well_formed_but_bodyless_document_xml_is_extractor_error() -> None:
    """The specific case behind F-8/F-15: a *valid* zip whose `word/document.xml` is well-formed
    XML but has no `<w:body>` element. `python-docx`'s `.paragraphs` property then does
    `document.element.body.p_lst`, and `body` is `None` — `AttributeError: 'NoneType' object has no
    attribute 'p_lst'` (verified empirically; not a `zipfile.BadZipFile`, not a
    `PackageNotFoundError`, so the two named translations above never catch it). Before the
    catch-all, this was an uncaught 500 with the file already on disk and no row — exactly the
    outcome ADR-0004 forbids; the end-to-end version of this case (201, row present, file stored) is
    `test_intake.py`'s `test_a_docx_with_a_malformed_document_xml_is_recorded_as_extractor_error`.
    """
    data = _read_fixture("sample.docx")
    malformed_document_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"</w:document>"
    )
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as source,
        zipfile.ZipFile(buffer, "w") as rebuilt,
    ):
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "word/document.xml":
                content = malformed_document_xml
            rebuilt.writestr(item, content)
    malformed = buffer.getvalue()

    with pytest.raises(CvExtractionFailed) as exc_info:
        await _extractor().extract(CvContentType.DOCX, malformed)

    assert exc_info.value.reason is ExtractionFailureReason.EXTRACTOR_ERROR


async def test_cancelled_error_still_propagates_and_is_not_recorded_as_a_failed_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`except Exception` (not `except BaseException`) in `extract()` is deliberate:
    `asyncio.CancelledError` is a `BaseException` since Python 3.8, so a genuinely cancelled request
    must keep cancelling rather than being swallowed and reported as `CvExtractionFailed`. This is
    the test that keeps someone from "tidying" that clause to `BaseException` — widening it would
    make this go green for the wrong reason (a cancellation silently becoming a recorded failure)
    while every other test in this module stays green too, which is exactly the kind of change
    nothing else here would catch.
    """
    extractor = PypdfDocxTextExtractor(timeout_seconds=10, max_pages=50)

    def _slow_extract_sync(
        self: PypdfDocxTextExtractor, content_type: CvContentType, data: bytes
    ) -> ExtractedText:
        time.sleep(0.5)
        return ExtractedText("x" * 300)

    monkeypatch.setattr(PypdfDocxTextExtractor, "_extract_sync", _slow_extract_sync)

    task = asyncio.ensure_future(extractor.extract(CvContentType.PDF, b"irrelevant"))
    await asyncio.sleep(0.05)  # let the thread actually start before cancelling it
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_dependency_error_from_pdfreader_construction_is_recorded_as_encrypted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-7: `PdfReader.__init__` auto-attempts an empty-password decrypt whenever a PDF is
    AES-encrypted, and without the optional `cryptography` package (not a dependency of this project
    — `api/pyproject.toml`) that raises `pypdf.errors.DependencyError` *from inside the
    constructor*, before `_extract_pdf`'s own `is_encrypted` branch ever runs. No AES-encrypted
    fixture exists in this corpus, and one cannot be built without `cryptography` installed in this
    container, so `PdfReader` itself is monkeypatched to raise the exact exception pypdf raises in
    that situation. The file genuinely is encrypted, so F-7's `encrypted` — not the catch-all's
    `extractor_error` — is the reason that lets the user act (remove the password), which is the
    whole point of this translation living on its own line rather than falling into the catch-all.
    """

    def _raise_dependency_error(*args: object, **kwargs: object) -> None:
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")

    monkeypatch.setattr(
        "tailorcraft.infrastructure.intake.extraction.PdfReader", _raise_dependency_error
    )

    with pytest.raises(EncryptedCvFile) as exc_info:
        await _extractor().extract(CvContentType.PDF, _read_fixture("sample.pdf"))

    assert exc_info.value.reason is ExtractionFailureReason.ENCRYPTED


# -----------------------------------------------------------------------------------------------
# The `cv_extraction.unexpected_error` log line (Constitution §8) — carries only `content_type`,
# `size_bytes` and `error_type`; `cv_extraction.finished`'s own field set is unchanged.
# -----------------------------------------------------------------------------------------------

# The AC-12-relevant payload of each event, deliberately excluding structlog's own bookkeeping
# fields (`event`, `level`, `timestamp`) — those are added by every log line in this codebase and
# are not part of what either docstring in `extraction.py` promises.
_STRUCTLOG_METADATA_FIELDS = {"event", "level", "timestamp"}
_FINISHED_LOG_FIELDS = {"content_type", "size_bytes", "duration_ms", "character_count", "outcome"}
_UNEXPECTED_ERROR_LOG_FIELDS = {"content_type", "size_bytes", "error_type"}


def _json_log_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Parse every captured record that is a JSON object — `configure_logging(Settings(app_env=
    "test"))` selects `JSONRenderer` (anything but `app_env="dev"`), so structlog's own lines
    decode cleanly; anything else captured (asyncio's selector notice, say) is silently skipped
    rather than failing the parse."""
    events = []
    for record in caplog.records:
        try:
            events.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            continue
    return events


async def test_unexpected_error_log_carries_only_content_type_size_bytes_and_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The privacy contract on the one path that logs an exception's *type*: never `str(exc)`
    (pypdf messages routinely quote raw document bytes — see the sweep test above), never
    `exc_info`/a traceback (which would keep the same bytes reachable via the frame's locals)."""
    data = _read_fixture("sample.docx")
    malformed_document_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"</w:document>"
    )
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as source,
        zipfile.ZipFile(buffer, "w") as rebuilt,
    ):
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "word/document.xml":
                content = malformed_document_xml
            rebuilt.writestr(item, content)
    malformed = buffer.getvalue()

    configure_logging(Settings(app_env="test"))
    with caplog.at_level(logging.INFO), pytest.raises(CvExtractionFailed):
        await _extractor().extract(CvContentType.DOCX, malformed)

    events = _json_log_events(caplog)
    unexpected_error_events = [
        e for e in events if e.get("event") == "cv_extraction.unexpected_error"
    ]
    assert len(unexpected_error_events) == 1, events
    event = unexpected_error_events[0]

    payload_fields = set(event) - _STRUCTLOG_METADATA_FIELDS
    assert payload_fields == _UNEXPECTED_ERROR_LOG_FIELDS
    assert event["error_type"] == "builtins.AttributeError"
    assert "p_lst" not in json.dumps(event)  # the exception's own message, absent by design
    assert "exc_info" not in event
    assert "exception" not in event
    assert "traceback" not in event


async def test_finished_log_field_set_is_unchanged_by_the_unexpected_error_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-12's contract for `cv_extraction.finished` predates this fix and must still hold exactly
    — the new information lives in the separate `unexpected_error` event above, not folded into this
    one's field set."""
    configure_logging(Settings(app_env="test"))
    with caplog.at_level(logging.INFO), pytest.raises(CvExtractionFailed):
        await _extractor().extract(CvContentType.PDF, _read_fixture("corrupt.pdf"))

    events = _json_log_events(caplog)
    finished_events = [e for e in events if e.get("event") == "cv_extraction.finished"]
    assert len(finished_events) == 1, events

    payload_fields = set(finished_events[0]) - _STRUCTLOG_METADATA_FIELDS
    assert payload_fields == _FINISHED_LOG_FIELDS
