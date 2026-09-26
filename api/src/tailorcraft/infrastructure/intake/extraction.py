"""`PypdfDocxTextExtractor` — the `CvTextExtractorPort` adapter (ADR-0009).

`pypdf` and `python-docx` are the **first feature libraries** in this codebase (`api/pyproject.toml`)
— they arrive with this slice, the first one that needs to read a CV, rather than being installed
speculatively.

Both libraries are synchronous and CPU-bound. Per ADR-0009 the call runs inline, in a worker thread
(`asyncio.to_thread`), wrapped in `asyncio.wait_for` with a hard timeout — never on the event loop,
and never queued to Celery. The 50-page cap on PDFs is checked *before* any page is parsed, so the
timeout is a backstop for a pathological file, not the mechanism that bounds ordinary work.

**Every exception leaving `extract()` is a `CvExtractionFailed` subclass**, guaranteed structurally
by a catch-all rather than by an allow-list of the library errors we happened to think of — see the
long comment on that `except Exception` clause for why the allow-list version was a bet that lost.

**Never log the extracted text or any fragment of it** (Constitution §8, AC-12). Every log line here
carries only `content_type`, `size_bytes`, `duration_ms`, `character_count`, `outcome` and — on the
unexpected-failure path only — the exception's fully-qualified *type*. Never a library error message:
`pypdf` quotes raw bytes from the document in several of its own.
"""

from __future__ import annotations

import asyncio
import time
import zipfile
from io import BytesIO
from typing import TYPE_CHECKING, Final, NoReturn

import structlog
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from pypdf import PdfReader
from pypdf.errors import DependencyError, FileNotDecryptedError, PdfReadError

from tailorcraft.domain.intake.errors import (
    CorruptCvFile,
    CvExtractionFailed,
    CvExtractionTimedOut,
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

if TYPE_CHECKING:
    from tailorcraft.domain.intake.ports import CvTextExtractorPort

log = structlog.get_logger(__name__)

# The constructor's fallback for `max_characters`, equal to `Settings.max_extracted_characters`'
# default. The composition root (`api/deps.py`) always passes the setting; this exists so a caller
# that predates the cap (1.1's adapter tests) still builds the adapter it meant to.
DEFAULT_MAX_EXTRACTED_CHARACTERS: Final = 250_000


class PypdfDocxTextExtractor:
    """Pulls text out of a PDF, DOCX or TXT file's bytes, entirely off the event loop.

    `max_pages`, `timeout_seconds` and `max_characters` come from `Settings.max_cv_pages` /
    `Settings.extraction_timeout_seconds` / `Settings.max_extracted_characters` — this adapter never
    reads the environment itself (Constitution §8: `os.environ` is read in exactly one place).

    **The character cap is checked while extracting, not after** (slice 2.2, T30b-C) — the page cap's
    idea one step later, and guarded egress's "enforce the byte cap while streaming" applied to text:
    a running count over the pieces already extracted, refused the moment it passes the cap, so an
    archive that inflates 171 KB into 39.5 M characters never becomes one string, one normalizing
    pass, one database value and one loop-side `ExtractedText` per load.

    What the count MEANS: **raw extracted characters** — exactly `len()` of the string this adapter
    would have handed to `ExtractedText`, separators included, before whitespace is collapsed. Not
    `ExtractedText.character_count` (normalized), on purpose: normalization only shrinks a string, so
    raw ≤ cap guarantees stored ≤ cap, and a running count needs no normalizing pass of its own.
    The price is that a file padded with whitespace can be refused at a normalized length under the
    cap; at 10 times what tailoring accepts, that file is not a CV we could have used.

    Granularity, stated rather than implied: one PDF page's text and one DOCX paragraph's text are
    each built whole before they are counted — the library hands them back as strings — so the
    bound is "the cap plus one piece", not the cap exactly. `python-docx` has also already parsed the
    whole `document.xml` into a tree by then; the cap bounds the text built from it and every copy
    downstream, not the parse. The 10 s timeout remains the backstop for that.
    """

    def __init__(
        self,
        timeout_seconds: int,
        max_pages: int,
        *,
        max_characters: int = DEFAULT_MAX_EXTRACTED_CHARACTERS,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_pages = max_pages
        self._max_characters = max_characters

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
        except Exception as exc:
            # THE CATCH-ALL, and it is load-bearing rather than defensive habit. `CvTextExtractorPort`
            # (domain/intake/ports.py) promises "a `CvExtractionFailed` subclass on **every**
            # failure", and `UploadBaseCv` catches only that — so any other exception escaping here
            # escapes the use case too and becomes a 500 with the file already on disk and no row,
            # the exact outcome ADR-0004 forbids ("a failed run is a recorded state, never a 500 with
            # nothing on disk"). Translating by allow-list makes that promise a bet on having
            # enumerated every way `pypdf` and `python-docx` can fail on a hostile file, and that bet
            # loses: a corruption sweep over one fixture produced `KeyError`, `AttributeError`,
            # `ValueError` and `pypdf.errors.LimitReachedError`, none of which is a `PdfReadError`.
            # The specific translations above still run first and still carry the better reason; this
            # is only the floor beneath them.
            #
            # `except Exception` (not `BaseException`) is deliberate: `asyncio.CancelledError` is a
            # `BaseException` in 3.8+, so a cancelled request still cancels rather than being
            # recorded as a failed extraction.
            #
            # Two privacy rules apply here and both are Constitution §8, not fussiness:
            #
            # 1. **Only the exception's TYPE is logged, never its message or the object.** A `pypdf`
            #    error message routinely quotes bytes lifted straight out of the document.
            # 2. **`from None`, not `from exc`.** This frame's locals include `data` — the CV — and a
            #    chained exception keeps the original's traceback, and therefore that frame, alive
            #    and reachable. `raise ... from None` sets `__suppress_context__`, which is what both
            #    the stdlib traceback formatter and `sentry_sdk`'s exception-chain walker honour, so
            #    the CV bytes cannot ride out with a report. The type in the log line above is the
            #    debugging thread to pull instead.
            self._log_unexpected_error(content_type, len(data), started_at, exc)
            raise CvExtractionFailed(ExtractionFailureReason.EXTRACTOR_ERROR) from None

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

    def _log_unexpected_error(
        self,
        content_type: CvContentType,
        size_bytes: int,
        started_at: float,
        exc: BaseException,
    ) -> None:
        """Record that an unrecognised library exception was translated to `EXTRACTOR_ERROR`.

        A separate event from `cv_extraction.finished` on purpose: the `finished` line's field set is
        AC-12's contract and stays exactly as it is, while this line adds the one extra fact an
        operator needs — the fully-qualified exception **type**, which is a class name and can carry
        no document content. `str(exc)` and `exc_info` are both absent by design; see the caller.
        """
        self._log_outcome(
            content_type, size_bytes, started_at, character_count=None, outcome="extractor_error"
        )
        log.warning(
            "cv_extraction.unexpected_error",
            content_type=content_type.value,
            size_bytes=size_bytes,
            error_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
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
            pages: list[str] = []
            # Running `len("\n".join(pages))`, kept without joining: each page's length plus one
            # for the separator before every page but the first.
            character_count = 0
            for page in reader.pages:
                page_text = page.extract_text()
                character_count += len(page_text) + (1 if pages else 0)
                if character_count > self._max_characters:
                    self._refuse_text_too_long()
                pages.append(page_text)
            return "\n".join(pages)
        except (FileNotDecryptedError, DependencyError) as exc:
            # `DependencyError` belongs on this line, not in the catch-all, and the reason is not
            # obvious from its name: `PdfReader.__init__` auto-attempts an empty-password decrypt
            # whenever `is_encrypted` is true, *before* the `is_encrypted` branch above ever runs.
            # For an AES-encrypted PDF that needs the optional `cryptography` package, which this
            # project does not install (`api/pyproject.toml`), pypdf raises `DependencyError` from
            # inside the constructor. The file genuinely is password-protected, so F-7's
            # `encrypted` is the honest reason and "remove the password" the actionable message —
            # routing it to the catch-all would tell a user with an ordinary encrypted CV to "try
            # again", which cannot work. It is also NOT a `PdfReadError` (its base is `Exception`),
            # which is precisely why the old allow-list missed it.
            #
            # SCOPE ASSUMPTION, and the reason this mapping is safe rather than merely convenient:
            # `pypdf` raises `DependencyError` from two places — the crypto fallbacks, and
            # `filters.py` for JBIG2 *image* decoding. Only the first is reachable here because the
            # `try` above calls nothing but `extract_text()`. ADR-0009 names OCR as a future re-open
            # of that decision, and the day someone extracts images inside this block, an
            # unencrypted scan will start telling its owner to "remove the password". Narrow this
            # clause then; it is correct only for as long as the sentence above stays true.
            raise EncryptedCvFile() from exc
        except PdfReadError as exc:
            raise CorruptCvFile() from exc

    def _extract_docx(self, data: bytes) -> str:
        try:
            document = Document(BytesIO(data))
            paragraphs: list[str] = []
            # Same running count as `_extract_pdf`'s, per paragraph.
            character_count = 0
            for paragraph in document.paragraphs:
                paragraph_text = paragraph.text
                character_count += len(paragraph_text) + (1 if paragraphs else 0)
                if character_count > self._max_characters:
                    self._refuse_text_too_long()
                paragraphs.append(paragraph_text)
            return "\n".join(paragraphs)
        except (zipfile.BadZipFile, PackageNotFoundError) as exc:
            raise CorruptCvFile() from exc

    def _decode_txt(self, data: bytes) -> str:
        text = self._decode_txt_bytes(data)
        # On the decoded text, in one piece: a TXT file has no pages or paragraphs to stop between,
        # and the decode is already bounded by the 10 MB upload cap (≤ 10 M characters either way).
        if len(text) > self._max_characters:
            self._refuse_text_too_long()
        return text

    def _decode_txt_bytes(self, data: bytes) -> str:
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

    def _refuse_text_too_long(self) -> NoReturn:
        """Refuse extracted text that has passed `max_characters` — called only once a running
        count is already over the cap, never on the ordinary path.

        SKELETON (T30b-C): raises `NotImplementedError`, which the `except Exception` floor in
        `extract` records as `EXTRACTOR_ERROR` — so an over-cap file fails with the wrong reason
        until GREEN, while every under-cap file extracts exactly as before.
        """
        raise NotImplementedError


if TYPE_CHECKING:
    # Proves `PypdfDocxTextExtractor` structurally satisfies `CvTextExtractorPort` without an
    # instance — never executed. See the identical pattern in
    # `infrastructure/persistence/repositories/identity/guest_session.py`.
    def _assert_implements_cv_text_extractor_port(extractor: PypdfDocxTextExtractor) -> None:
        _: CvTextExtractorPort = extractor
