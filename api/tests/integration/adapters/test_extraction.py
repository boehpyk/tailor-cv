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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
from docx import Document
from docx.document import Document as DocxDocument
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from pypdf import PdfWriter
from pypdf.errors import DependencyError
from pypdf.generic import ContentStream, DictionaryObject, NameObject

from tailorcraft.domain.intake.errors import (
    CorruptCvFile,
    CvExtractionFailed,
    CvHasNoTextLayer,
    CvHasTooManyPages,
    CvTextTooLong,
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


# ==================================================================================================
# T30b-C (slice 2.2, technical-plan.md's 2026-09-26 amendment, option C) — the extracted-character
# cap. RED half of a red-first cycle: `_refuse_text_too_long()` (extraction.py) is a SKELETON that
# raises `NotImplementedError`, which the `except Exception` floor in `extract()` converts to
# `CvExtractionFailed(EXTRACTOR_ERROR)` — so every test below that asserts `CvTextTooLong` /
# `TEXT_TOO_LONG` is written against the spec (the amendment's "C" bullet), not against that
# placeholder body, and is expected to fail on the reason mismatch
# (`ExtractionFailureReason.EXTRACTOR_ERROR != ExtractionFailureReason.TEXT_TOO_LONG`) — never on an
# ImportError or a missing fixture.
#
# What the count means (extraction.py's own docstring): raw extracted characters, i.e. `len()` of
# the string this adapter would hand to `ExtractedText` — separators included, whitespace not yet
# collapsed — checked per PDF page, per DOCX paragraph, and on the decoded TXT. Every fixture below
# is built from literal ASCII letters (never whitespace), so raw length and non-whitespace length
# coincide and the 200-character floor (`ExtractedText`) is cleared by a wide margin whenever the
# cap under test is (as here) three orders of magnitude above it.
# ==================================================================================================

_CAP = 1_000  # small and explicit, per CLAUDE.md's "small-cap override" convention


def _extractor_with_cap(max_characters: int) -> PypdfDocxTextExtractor:
    return PypdfDocxTextExtractor(timeout_seconds=10, max_pages=50, max_characters=max_characters)


def _txt_bytes(character_count: int) -> bytes:
    return ("A" * character_count).encode("utf-8")


def _docx_bytes_with_paragraphs(paragraph_lengths: list[int]) -> bytes:
    """A DOCX whose paragraphs, read back through `python-docx`, have exactly the given lengths —
    each paragraph a single run of `A`s, so `Paragraph.text` returns it unchanged (verified
    empirically: a fresh `Document()` with only `add_paragraph` calls has no extra paragraphs, and
    round-tripping through bytes preserves each paragraph's text exactly)."""
    document = Document()
    for length in paragraph_lengths:
        document.add_paragraph("A" * length)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf_bytes_with_page_texts(page_texts: list[str]) -> bytes:
    """A PDF with one page per string in `page_texts`, each page's `extract_text()` returning that
    string character-for-character — built from a raw `BT`/`Td`/`Tj` content stream against the
    standard (unembedded) Helvetica font, the same technique `tests/fixtures/cvs/README.md` says
    built `sample.pdf`. Verified empirically (T30b-C): a single `Tj` string round-trips through
    `PdfWriter` -> bytes -> `PdfReader` -> `extract_text()` with no characters added or dropped —
    `add_blank_page`'s pages (used elsewhere in this file for the page-cap tests) have no text layer
    at all and cannot stand in for this."""
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)  # no public "add a resource" API exists
    for text_value in page_texts:
        page = writer.add_blank_page(width=200, height=200)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        escaped = text_value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content = ContentStream(None, writer)
        content.set_data(f"BT /F1 10 Tf 5 100 Td ({escaped}) Tj ET".encode("latin-1"))
        page.replace_contents(content)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- Over the cap: exactly `CvTextTooLong`, reason `TEXT_TOO_LONG`, for each content type ---------

_OVER_CAP_CASES = [
    pytest.param(CvContentType.TXT, id="txt"),
    pytest.param(CvContentType.DOCX, id="docx"),
    pytest.param(CvContentType.PDF, id="pdf"),
]


def _over_cap_bytes(content_type: CvContentType) -> bytes:
    if content_type is CvContentType.TXT:
        return _txt_bytes(_CAP + 1)
    if content_type is CvContentType.DOCX:
        return _docx_bytes_with_paragraphs([_CAP + 1])
    return _pdf_bytes_with_page_texts(["A" * (_CAP + 1)])


@pytest.mark.parametrize("content_type", _OVER_CAP_CASES)
async def test_text_one_character_over_the_cap_raises_cv_text_too_long(
    content_type: CvContentType,
) -> None:
    data = _over_cap_bytes(content_type)
    extractor = _extractor_with_cap(_CAP)

    with pytest.raises(CvExtractionFailed) as exc_info:
        await extractor.extract(content_type, data)

    assert exc_info.value.reason is ExtractionFailureReason.TEXT_TOO_LONG
    assert type(exc_info.value) is CvTextTooLong


# --- Exactly at the cap: boundary is strict `>`, so the cap itself is accepted ---------------------


def _at_cap_bytes(content_type: CvContentType) -> bytes:
    if content_type is CvContentType.TXT:
        return _txt_bytes(_CAP)
    if content_type is CvContentType.DOCX:
        return _docx_bytes_with_paragraphs([_CAP])
    return _pdf_bytes_with_page_texts(["A" * _CAP])


@pytest.mark.parametrize("content_type", _OVER_CAP_CASES)
async def test_text_exactly_at_the_cap_is_accepted(content_type: CvContentType) -> None:
    data = _at_cap_bytes(content_type)
    extractor = _extractor_with_cap(_CAP)

    result = await extractor.extract(content_type, data)

    assert isinstance(result, ExtractedText)
    # Every character is a non-whitespace `A`, so the normalized count equals the raw count exactly
    # — the cap itself, never one more and never fewer.
    assert result.character_count == _CAP


# --- Under the cap: ordinary extraction, unaffected by the cap being configured --------------------
#
# Already true today (not a red): nothing calls `_refuse_text_too_long()` unless the running count
# has already passed `max_characters`, so a file that never reaches the cap takes exactly the same
# path it always has. Recorded anyway, per CLAUDE.md's I-47 precedent ("a red-first task that only
# ever asserts things which already pass would be an easy way to fail to notice the bound quietly
# disappearing later").


@pytest.mark.parametrize("content_type", _OVER_CAP_CASES)
async def test_text_comfortably_under_the_cap_is_unaffected(content_type: CvContentType) -> None:
    under_cap = _CAP - 200
    if content_type is CvContentType.TXT:
        data = _txt_bytes(under_cap)
    elif content_type is CvContentType.DOCX:
        data = _docx_bytes_with_paragraphs([under_cap])
    else:
        data = _pdf_bytes_with_page_texts(["A" * under_cap])
    extractor = _extractor_with_cap(_CAP)

    result = await extractor.extract(content_type, data)

    assert isinstance(result, ExtractedText)
    assert result.character_count == under_cap


# --- DOCX: the cap is enforced per paragraph, stopping the loop early, not after the whole document
# is read into one string ---------------------------------------------------------------------------


async def test_docx_with_many_paragraphs_stops_reading_paragraphs_once_the_cap_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`document.paragraphs` (python-docx) hands back the whole paragraph list up front — the cap
    cannot stop *that* — but a paragraph's runs are only walked when its block is pulled, through
    `Paragraph.iter_inner_content` (the Markdown path, ADR-0044 §2, no longer reads
    `Paragraph.text`, so counting that read proved nothing). 300 fifty-character paragraphs, cap
    1_000: the running count (50 for the first, 52 for every one after: the blank line) crosses
    1_000 at about the 19th block, so the assertion is "reads at most a handful of paragraphs out of
    300", counted by wrapping the real method. Proven by mutation: materialising every block before
    joining (`list(...)`) makes the count 300 and this test fail.
    """
    paragraph_length = 50
    paragraph_count = 300
    data = _docx_bytes_with_paragraphs([paragraph_length] * paragraph_count)

    accessed = 0
    real_iter_inner_content = Paragraph.iter_inner_content

    def _counting_iter_inner_content(self: Paragraph) -> Iterator[object]:
        nonlocal accessed
        accessed += 1
        return cast("Iterator[object]", real_iter_inner_content(self))

    monkeypatch.setattr(Paragraph, "iter_inner_content", _counting_iter_inner_content)

    extractor = _extractor_with_cap(_CAP)
    with pytest.raises(CvExtractionFailed) as exc_info:
        await extractor.extract(CvContentType.DOCX, data)

    assert exc_info.value.reason is ExtractionFailureReason.TEXT_TOO_LONG
    assert 0 < accessed < 25, (
        f"expected the cap to stop paragraph reads well short of all {paragraph_count}; "
        f"{accessed} paragraphs' inner content was walked"
    )


# ==================================================================================================
# tailoring-preserve-cv-formatting (T7, test-after): ExtractedText keeps lines (AC-5), a DOCX is
# extracted as Markdown (AC-6, AC-7), with a best-effort fallback (AC-8) and a cap that counts the
# markup (AC-9). Every expected string below is written by hand from the spec, never copied from a
# run of the adapter.
# ==================================================================================================

_FILLER = "F" * 250  # clears the 200-non-whitespace floor so a small document is extractable
_FALLBACK_LOG_FIELDS = {"content_type", "size_bytes", "error_type"}


def _docx_from(build: Callable[[DocxDocument], None]) -> bytes:
    document = Document()
    build(document)
    document.add_paragraph(_FILLER)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def _markdown_of(build: Callable[[DocxDocument], None]) -> str:
    """The extracted text of the built document, minus the trailing filler block."""
    result = await _extractor().extract(CvContentType.DOCX, _docx_from(build))
    suffix = "\n\n" + _FILLER
    assert result.value.endswith(suffix)
    return result.value[: -len(suffix)]


def _add_runs(
    document: DocxDocument, runs: list[tuple[str, bool]], style: str | None = None
) -> Paragraph:
    paragraph = document.add_paragraph(style=style)
    for text, bold in runs:
        paragraph.add_run(text).bold = True if bold else None
    return paragraph


# --- AC-6: headings -------------------------------------------------------------------------------


async def test_docx_title_and_headings_become_markdown_headings_and_level_seven_clamps_to_six() -> (
    None
):
    def build(document: DocxDocument) -> None:
        document.add_paragraph("Alex Doe", style="Title")
        document.add_paragraph("Experience", style="Heading 1")
        document.add_paragraph("Acme", style="Heading 2")
        document.add_paragraph("Deep", style="Heading 7")

    assert await _markdown_of(build) == "# Alex Doe\n\n# Experience\n\n## Acme\n\n###### Deep"


async def test_a_style_named_heading_zero_is_plain_text() -> None:
    def build(document: DocxDocument) -> None:
        document.styles.add_style("Heading 0", WD_STYLE_TYPE.PARAGRAPH)
        document.add_paragraph("Not a heading", style="Heading 0")

    assert await _markdown_of(build) == "Not a heading"


async def test_empty_headings_and_empty_bold_runs_emit_no_markup() -> None:
    def build(document: DocxDocument) -> None:
        document.add_paragraph("before")
        document.add_paragraph("", style="Heading 1")
        document.add_paragraph("   ", style="Heading 2")
        _add_runs(document, [("", True)])
        _add_runs(document, [("  ", True)])
        document.add_paragraph("after")

    assert await _markdown_of(build) == "before\n\nafter"


# --- AC-6: bold -----------------------------------------------------------------------------------


async def test_a_bold_run_inside_a_sentence_is_wrapped_in_asterisks() -> None:
    def build(document: DocxDocument) -> None:
        _add_runs(document, [("Led the ", False), ("team", True), (" well", False)])

    assert await _markdown_of(build) == "Led the **team** well"


async def test_adjacent_bold_runs_are_merged_into_one_span() -> None:
    def build(document: DocxDocument) -> None:
        _add_runs(document, [("Led ", False), ("the ", True), ("team", True)])

    assert await _markdown_of(build) == "Led **the team**"


async def test_the_spaces_around_a_bold_run_stay_outside_the_markers() -> None:
    def build(document: DocxDocument) -> None:
        _add_runs(document, [("Led", False), (" the team ", True), ("now", False)])

    assert await _markdown_of(build) == "Led **the team** now"


# --- AC-6: lists ----------------------------------------------------------------------------------


async def test_list_items_become_dashes_joined_by_one_newline_and_a_normal_block_follows_a_blank_line() -> (
    None
):
    def build(document: DocxDocument) -> None:
        document.add_paragraph("Duties")
        document.add_paragraph("one", style="List Bullet")
        document.add_paragraph("two", style="List Bullet")
        document.add_paragraph("three", style="List Number")
        document.add_paragraph("done")

    assert await _markdown_of(build) == "Duties\n\n- one\n- two\n- three\n\ndone"


# --- AC-7: nothing is escaped ---------------------------------------------------------------------


async def test_literal_markdown_characters_in_docx_text_pass_through_unescaped() -> None:
    literal = "5* rating, snake_case, # hash, C# / .NET, `tick`, [bracket], back\\slash"

    def build(document: DocxDocument) -> None:
        document.add_paragraph(literal)

    assert await _markdown_of(build) == literal


# --- A hyperlinked run is kept (python-docx 1.2.0's `Paragraph.runs` skips it) ---------------------


async def test_a_hyperlinked_email_is_kept_in_the_text() -> None:
    def build(document: DocxDocument) -> None:
        paragraph = document.add_paragraph("Mail ")
        paragraph._p.append(
            parse_xml(
                f"<w:hyperlink {nsdecls('w')}><w:r><w:t>a@example.com</w:t></w:r></w:hyperlink>"
            )
        )
        paragraph.add_run(" today")

    assert await _markdown_of(build) == "Mail a@example.com today"


# --- AC-5 / AC-10: the committed corpus keeps its lines -------------------------------------------


def _sample_txt_lines() -> list[str]:
    return (FIXTURES_DIR / "sample.txt").read_text(encoding="utf-8").splitlines()


async def test_sample_txt_keeps_its_lines() -> None:
    result = await _extractor().extract(CvContentType.TXT, _read_fixture("sample.txt"))

    assert result.value.splitlines() == _sample_txt_lines()


async def test_sample_pdf_keeps_its_lines() -> None:
    result = await _extractor().extract(CvContentType.PDF, _read_fixture("sample.pdf"))

    assert result.value.startswith("Alex Rivera\nBackend Engineer\nalex.rivera@example.com")
    assert len(result.value.splitlines()) > 5


async def test_sample_docx_is_its_paragraphs_separated_by_blank_lines_with_no_markup() -> None:
    result = await _extractor().extract(CvContentType.DOCX, _read_fixture("sample.docx"))

    # The fixture's text is `sample.txt`'s (README.md); the DOCX has one paragraph per non-empty line.
    expected = "\n\n".join(line for line in _sample_txt_lines() if line.strip())
    assert result.value == expected
    assert "*" not in result.value
    assert "#" not in result.value


# --- AC-8: the formatting fallback ----------------------------------------------------------------


def _formatted_docx() -> bytes:
    def build(document: DocxDocument) -> None:
        document.add_paragraph("Experience", style="Heading 1")
        _add_runs(document, [("Led the ", False), ("team", True)])
        document.add_paragraph("shipped", style="List Bullet")

    return _docx_from(build)


def _raise_runtime_error(self: Run) -> bool | None:
    raise RuntimeError("secret-cv-text")


async def test_a_style_or_run_that_raises_falls_back_to_plain_paragraph_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _formatted_docx()
    monkeypatch.setattr(Run, "bold", property(_raise_runtime_error))

    result = await _extractor().extract(CvContentType.DOCX, data)

    assert result.value == f"Experience\n\nLed the team\n\nshipped\n\n{_FILLER}"


async def test_the_fallback_logs_one_line_with_only_type_size_and_error_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    data = _formatted_docx()
    monkeypatch.setattr(Run, "bold", property(_raise_runtime_error))

    configure_logging(Settings(app_env="test"))
    with caplog.at_level(logging.INFO):
        await _extractor().extract(CvContentType.DOCX, data)

    events = _json_log_events(caplog)
    fallbacks = [e for e in events if e.get("event") == "cv_extraction.docx_formatting_fallback"]
    assert len(fallbacks) == 1, events
    event = fallbacks[0]
    assert set(event) - _STRUCTLOG_METADATA_FIELDS == _FALLBACK_LOG_FIELDS
    assert event["content_type"] == CvContentType.DOCX.value
    assert event["size_bytes"] == len(data)
    assert event["error_type"] == "builtins.RuntimeError"
    assert "secret-cv-text" not in json.dumps(events)
    assert not [e for e in events if e.get("event") == "cv_extraction.unexpected_error"]
    for record in caplog.records:
        assert record.exc_info is None


async def test_a_cv_extraction_failure_inside_the_formatting_pass_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    data = _formatted_docx()

    def _refuse(self: Run) -> bool | None:
        raise CvTextTooLong()

    monkeypatch.setattr(Run, "bold", property(_refuse))

    configure_logging(Settings(app_env="test"))
    with caplog.at_level(logging.INFO), pytest.raises(CvTextTooLong):
        await _extractor().extract(CvContentType.DOCX, data)

    events = _json_log_events(caplog)
    assert not [e for e in events if e.get("event") == "cv_extraction.docx_formatting_fallback"]


# --- AC-9: the cap counts the emitted Markdown ----------------------------------------------------


def _bold_paragraph_docx(length: int) -> bytes:
    document = Document()
    _add_runs(document, [("A" * length, True)])
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def test_a_document_under_the_cap_as_plain_text_but_over_it_as_markdown_is_text_too_long(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 300 characters of text; 304 once wrapped in `**…**`. Cap 302 sits between the two.
    data = _bold_paragraph_docx(300)

    configure_logging(Settings(app_env="test"))
    with caplog.at_level(logging.INFO), pytest.raises(CvExtractionFailed) as exc_info:
        await _extractor_with_cap(302).extract(CvContentType.DOCX, data)

    assert exc_info.value.reason is ExtractionFailureReason.TEXT_TOO_LONG
    events = _json_log_events(caplog)
    assert not [e for e in events if e.get("event") == "cv_extraction.docx_formatting_fallback"]


async def test_a_markdown_document_exactly_at_the_cap_is_accepted() -> None:
    result = await _extractor_with_cap(304).extract(CvContentType.DOCX, _bold_paragraph_docx(300))

    assert result.value == "**" + "A" * 300 + "**"


# --- AC-9: `cv_extraction.finished` keeps its field set -------------------------------------------


async def test_finished_log_field_set_is_unchanged_for_a_markdown_docx(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging(Settings(app_env="test"))
    with caplog.at_level(logging.INFO):
        await _extractor().extract(CvContentType.DOCX, _formatted_docx())

    finished = [e for e in _json_log_events(caplog) if e.get("event") == "cv_extraction.finished"]
    assert len(finished) == 1
    assert set(finished[0]) - _STRUCTLOG_METADATA_FIELDS == _FINISHED_LOG_FIELDS
    assert finished[0]["outcome"] == "success"
