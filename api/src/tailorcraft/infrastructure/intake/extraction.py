"""`PypdfDocxTextExtractor` — the `CvTextExtractorPort` adapter (ADR-0009).

`pypdf` and `python-docx` are the **first feature libraries** in this codebase (`api/pyproject.toml`)
— they arrive with this slice, the first one that needs to read a CV, rather than being installed
speculatively.

Both libraries are synchronous and CPU-bound. Per ADR-0009 the call runs inline, in a worker thread
(`asyncio.to_thread`), wrapped in `asyncio.wait_for` with a hard timeout — never on the event loop,
and never queued to Celery. The 50-page cap on PDFs is checked *before* any page is parsed, so the
timeout is a backstop for a pathological file, not the mechanism that bounds ordinary work.

**Never log the extracted text or any fragment of it** (Constitution §8, AC-12). Every log line here
carries only `content_type`, `size_bytes`, `duration_ms`, `character_count` and `outcome`.
"""

from __future__ import annotations

import asyncio
import time
import zipfile
from io import BytesIO
from typing import TYPE_CHECKING

import structlog
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

from tailorcraft.domain.intake.errors import (
    CorruptCvFile,
    CvExtractionFailed,
    CvExtractionTimedOut,
    CvHasNoTextLayer,
    CvHasTooManyPages,
    CvTextTooShort,
    EncryptedCvFile,
)
from tailorcraft.domain.intake.value_objects import CvContentType, ExtractedText

if TYPE_CHECKING:
    from tailorcraft.domain.intake.ports import CvTextExtractorPort

log = structlog.get_logger(__name__)


class PypdfDocxTextExtractor:
    """Pulls text out of a PDF, DOCX or TXT file's bytes, entirely off the event loop.

    `max_pages` and `timeout_seconds` come from `Settings.max_cv_pages` /
    `Settings.extraction_timeout_seconds` — this adapter never reads the environment itself
    (Constitution §8: `os.environ` is read in exactly one place).
    """

    def __init__(self, timeout_seconds: int, max_pages: int) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_pages = max_pages

    async def extract(self, content_type: CvContentType, data: bytes) -> ExtractedText:
        started_at = time.monotonic()
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(self._extract_sync, content_type, data),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            # `wait_for` cancels the *await*, not the thread (F-12, ADR-0009 "Consequences"):
            # `asyncio.to_thread` has no way to kill the worker thread underneath it, so a
            # pathological file keeps a thread pool slot busy after this coroutine has already
            # raised. Pretending `wait_for` stopped the work would be the lie that makes this
            # "timeout" not actually bound anything — it bounds the *request*, not the CPU.
            self._log_outcome(
                content_type, len(data), started_at, character_count=None, outcome="timed_out"
            )
            raise CvExtractionTimedOut() from exc
        except CvExtractionFailed as exc:
            self._log_outcome(
                content_type, len(data), started_at, character_count=None, outcome=exc.reason.value
            )
            raise

        self._log_outcome(
            content_type,
            len(data),
            started_at,
            character_count=text.character_count,
            outcome="success",
        )
        return text

    def _log_outcome(
        self,
        content_type: CvContentType,
        size_bytes: int,
        started_at: float,
        *,
        character_count: int | None,
        outcome: str,
    ) -> None:
        duration_ms = round((time.monotonic() - started_at) * 1000)
        log.info(
            "cv_extraction.finished",
            content_type=content_type.value,
            size_bytes=size_bytes,
            duration_ms=duration_ms,
            character_count=character_count,
            outcome=outcome,
        )

    # -- synchronous work, run only via `asyncio.to_thread` above ------------

    def _extract_sync(self, content_type: CvContentType, data: bytes) -> ExtractedText:
        if content_type is CvContentType.PDF:
            raw_text = self._extract_pdf(data)
            structural_no_text_layer = True
        elif content_type is CvContentType.DOCX:
            raw_text = self._extract_docx(data)
            structural_no_text_layer = True
        else:
            raw_text = self._decode_txt(data)
            # A TXT file has no separate "layer" to be missing — the bytes *are* the content, so a
            # blank one is simply short, not structurally empty the way a scanned PDF is. Both
            # outcomes still need a `CvExtractionFailed` subclass to cross `CvTextExtractorPort`
            # (its docstring is explicit: never a bare exception) — see the DECISION note below.
            structural_no_text_layer = False

        non_whitespace_count = sum(1 for char in raw_text if not char.isspace())

        if structural_no_text_layer and non_whitespace_count == 0:
            raise CvHasNoTextLayer()

        # DECISION (made here, on purpose): the technical plan's translation table reads
        # "whitespace-only result -> CvHasNoTextLayer (PDF/DOCX) or EmptyExtraction (TXT)". Taken
        # literally, a blank TXT file would raise `EmptyExtraction` — a `DomainError`, but *not* a
        # `CvExtractionFailed` subclass. `CvTextExtractorPort.extract` (domain/intake/ports.py)
        # is explicit that this method raises a `CvExtractionFailed` subclass on every failure,
        # and `UploadBaseCv.__call__` (application/intake/upload_base_cv.py) only ever catches
        # `CvExtractionFailed` — a bare `EmptyExtraction` would escape both, breaking ADR-0004's
        # rule that a failed run is a recorded state, never an uncaught exception. The domain
        # port's contract wins: a whitespace-only TXT is treated as the zero-character case of
        # "too short" below, not as a distinct raised type. Non-whitespace count of zero and one of
        # 150 both fail the same 200-character floor for the same reason, so folding them together
        # costs nothing in either accuracy or the failure message the user sees.
        if non_whitespace_count < 200:
            raise CvTextTooShort()

        return ExtractedText(raw_text)

    def _extract_pdf(self, data: bytes) -> str:
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise EncryptedCvFile()
            # Refused BEFORE any page's content is parsed (ADR-0009 §2) — enumerating `.pages`
            # walks the page tree, not each page's content stream, so this stays cheap even for a
            # file this branch is about to reject.
            if len(reader.pages) > self._max_pages:
                raise CvHasTooManyPages()
            return "\n".join(page.extract_text() for page in reader.pages)
        except FileNotDecryptedError as exc:
            raise EncryptedCvFile() from exc
        except PdfReadError as exc:
            raise CorruptCvFile() from exc

    def _extract_docx(self, data: bytes) -> str:
        try:
            document = Document(BytesIO(data))
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        except (zipfile.BadZipFile, PackageNotFoundError) as exc:
            raise CorruptCvFile() from exc

    def _decode_txt(self, data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError as exc:
            # Sniffing already checked decodability on the first 8 KiB (infrastructure/intake/
            # sniffing.py); a failure here means bytes further into the file do not decode, which
            # is exactly the "not actually what it claims to be" situation `CorruptCvFile` names.
            raise CorruptCvFile() from exc


if TYPE_CHECKING:
    # Proves `PypdfDocxTextExtractor` structurally satisfies `CvTextExtractorPort` without an
    # instance — never executed. See the identical pattern in
    # `infrastructure/persistence/repositories/identity/guest_session.py`.
    def _assert_implements_cv_text_extractor_port(extractor: PypdfDocxTextExtractor) -> None:
        _: CvTextExtractorPort = extractor
