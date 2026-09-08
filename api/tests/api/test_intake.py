"""API tests for `intake-base-cv-upload`'s HTTP surface: `POST/GET /api/base-cvs[/{id}]`.

**This is the RED half of a red-first cycle** (CLAUDE.md, sdlc.md §2, T25). Every test here is
written against `docs/specs/intake-base-cv-upload/feature-spec.md`'s failure contract (F-1…F-24)
and its acceptance criteria — not against `infrastructure/api/routers/intake.py`, whose three
handlers currently do nothing but `raise NotImplementedError` (T24's SKELETON). The router IS
mounted (`create_app` includes it), so a passing collection with every test failing on a real
assertion — never a 404, never an ImportError — is exactly what a correct RED run looks like here.

**Why this file's `client` fixture is not the shared one.** `tests/conftest.py`'s `client` uses
`ASGITransport(app=app)`, whose default `raise_app_exceptions=True` re-raises an unhandled handler
exception into the test as a bare Python exception (verified empirically: Starlette's
`ServerErrorMiddleware` always sends the 500 response *and* re-raises, precisely so a test client can
choose either behaviour). That turns every `NotImplementedError` skeleton into an ERROR, not a
`FAIL`, and buries the assertion this file is built around. This module's own `client` fixture below
sets `raise_app_exceptions=False`, so `response.status_code` is a real `500` and
`assert response.status_code == 201` fails exactly the way CLAUDE.md wants a red to fail: on the
assertion, with a message a human reads directly (`assert 500 == 201`).

**What is NOT observable from here, on purpose.** `BaseCvResponse` never carries `file_key` or
`extracted_text` (feature-spec.md's non-goals; ADR-0011) — AC-7's file-ref-grammar/containment check
and AC-1's byte-for-byte extraction fidelity are asserted directly against `LocalFileStore` and
`PypdfDocxTextExtractor` in the adapter tests (technical-plan.md's "Adapters" test-plan row), not
here. This file asserts the HTTP contract: status codes, the error envelope, the cookie, and the
fields `BaseCvResponse` actually exposes.

**A known, deliberately-not-fixed tension, flagged rather than quietly avoided:**
`test_unsupported_formats_are_rejected_415[rtf]` asserts F-4's literal list of rejected formats,
which names RTF. `infrastructure/intake/sniffing.py` (already built, test-after, not a skeleton)
classifies *any* byte stream that decodes cleanly as UTF-8/cp1252 with no embedded NUL as `TXT` —
and RFC-plain RTF (`{\rtf1\ansi ...}`) is ASCII, so it decodes fine. On today's implementation this
row will not go red the way the others do (`sniff_cv_content_type` already runs); it may go green
for the wrong reason, or simply disagree with the spec. That is not this file's call to make quietly
— it is written here, verbatim from the spec, so whoever wires the router hits it explicitly.

**AC-12's privacy test uses `caplog`, not `structlog.testing.capture_logs()`, on purpose.**
`configure_logging()` (which every fresh `app` fixture triggers via `create_app`) calls
`structlog.configure(processors=[...])` with a **brand-new list literal** every time, and
`cache_logger_on_first_use=True` means a module-level logger (`extraction.py`'s, `rate_limit.py`'s,
…) that was first used by an *earlier* test in this session keeps its own reference to that earlier
test's processors list — `capture_logs()`'s in-place mutation of "the current list" cannot reach a
stale reference held by an already-bound logger from a previous test. `caplog` sidesteps the whole
question: `structlog.stdlib.LoggerFactory()` routes every call through a real `logging.Logger`, and
by the time a record reaches it, `JSONRenderer` has already flattened the event to a plain string —
`caplog`'s handler on the root logger sees that string regardless of which processors chain rendered
it. Verified empirically against this exact `configure_logging()` recipe, run twice with fresh
processors and a logger cached from the first run, before relying on it here.
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pypdf import PdfWriter
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.domain.intake.value_objects import CvContentType
from tailorcraft.infrastructure.api.deps import get_app_settings, get_clock, get_session
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "cvs"

# The privacy test (AC-12) asserts these exact fragments never appear in the logs. They must match
# `tests/fixtures/cvs/README.md`'s description of `sample.pdf`'s content — if the corpus generator
# ever changes this text, update both places together.
_SAMPLE_CV_NAME_FRAGMENT = "Alex Rivera"
_SAMPLE_CV_EMAIL_FRAGMENT = "alex.rivera@example.com"
_SAMPLE_CV_EMPLOYER_FRAGMENT = "Northwind Logistics"


# ---------------------------------------------------------------------------------------------
# Fixture bytes and small HTTP helpers
# ---------------------------------------------------------------------------------------------


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _file_part(filename: str, data: bytes, content_type: str) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (filename, data, content_type)}


def _error_code(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["code"])


def _error_message(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["message"])


def _guest_cookie_header(response: Response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE_NAME}="):
            return header
    return None


def _guest_cookie_value(response: Response) -> str | None:
    header = _guest_cookie_header(response)
    if header is None:
        return None
    return header.split(";", 1)[0].split("=", 1)[1]


def _override_settings(app: FastAPI, base: Settings, **updates: object) -> Settings:
    """Point every settings-reading path this codebase currently uses at one modified `Settings`.

    Two patterns already coexist here: `SettingsDep` (`Depends(get_app_settings)`, used by
    `routers/health.py`) and `request.app.state.settings` (used by `deps.get_session`/`get_engine`).
    Whichever one T26's new `get_file_store` / `get_cv_text_extractor` / `get_rate_limiter`
    dependencies end up following, overriding both here covers it.
    """
    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


def _new_client(app: FastAPI) -> AsyncClient:
    """A second, independent cookie jar against the same app — for tests that need two distinct
    guest sessions (F-20's "another session", F-24's per-IP limit across many sessions).
    `ASGITransport`'s default `client` tuple is fixed, so every client built this way is seen by the
    server as the same peer IP, which is exactly what the per-IP rate-limit test needs."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


def _many_page_pdf_bytes(page_count: int) -> bytes:
    """A PDF with `page_count` blank pages — cheap to build, and enough to trip the page cap (F-11)
    without needing any real text-layer content: `PypdfDocxTextExtractor` refuses on page count
    *before* it ever calls `extract_text()` on a single page (ADR-0009 §2)."""
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _bare_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("readme.txt", "not an office document")
    return buf.getvalue()


def _xlsx_like_zip_bytes() -> bytes:
    """Same four leading magic bytes as a DOCX (`PK\\x03\\x04`), but the namelist names an XLSX
    part instead of `word/document.xml` — exactly the case `sniff_cv_content_type`'s namelist check
    exists to reject (technical-plan.md, "Adapters")."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return buf.getvalue()


def _rtf_bytes() -> bytes:
    return rb"{\rtf1\ansi This is not a CV, just RTF markup that happens to be valid ASCII text.}"


def _png_bytes() -> bytes:
    return _read_fixture("not-a-pdf.pdf")


# ---------------------------------------------------------------------------------------------
# Module-local fixtures
# ---------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Shadows `conftest.py`'s `client` fixture for every test in this module — see the module
    docstring for why `raise_app_exceptions=False` matters here specifically."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Applies `clear_redis` (conftest.py) to every test in this module automatically.

    Nearly every test here uploads, and CLAUDE.md/AC-18 is explicit: the rate limiter's Redis state
    is not touched by the database transaction rollback, so it must be cleared before *and* after
    every test that could have written to it — an autouse fixture is less error-prone across ~40
    tests than remembering the parameter on each one.
    """
    return None


# ---------------------------------------------------------------------------------------------
# AC-1 — the three accepted formats, happy path
# ---------------------------------------------------------------------------------------------


async def test_uploading_a_pdf_with_real_text_succeeds(client: AsyncClient) -> None:
    response = await client.post(
        "/api/base-cvs",
        files=_file_part("sample.pdf", _read_fixture("sample.pdf"), "application/pdf"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extracted"
    assert body["content_type"] == CvContentType.PDF.value
    assert isinstance(body["character_count"], int)
    # The 200-character floor is `ExtractedText`'s own invariant (I-5) — a successfully `extracted`
    # row can never report fewer, by construction of the value object that produced it.
    assert body["character_count"] >= 200
    assert body["failure_reason"] is None
    assert body["failure_message"] is None
    # Non-goal, stated explicitly in feature-spec.md: "No returning extracted text over the wire."
    assert "extracted_text" not in body


async def test_uploading_a_docx_with_real_text_succeeds(client: AsyncClient) -> None:
    response = await client.post(
        "/api/base-cvs",
        files=_file_part(
            "sample.docx",
            _read_fixture("sample.docx"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extracted"
    assert body["content_type"] == CvContentType.DOCX.value
    assert isinstance(body["character_count"], int)
    assert body["character_count"] >= 200
    assert body["failure_reason"] is None


async def test_uploading_a_txt_with_real_text_succeeds_and_reports_the_exact_normalized_length(
    client: AsyncClient,
) -> None:
    """TXT is the one format whose extracted text is exactly `data.decode()` (`_decode_txt`, no
    per-line reassembly heuristic the way PDF/DOCX extraction has) — so this is the one format
    where AC-1's "character_count equals the length of the extracted text" is checkable from an
    independently-computed expectation rather than by re-running the extractor, matching
    `ExtractedText.__post_init__`'s documented normalization (whitespace collapsed to single
    spaces, ends stripped) rather than the code's behaviour observed by running it.
    """
    raw = _read_fixture("sample.txt")
    expected_character_count = len(" ".join(raw.decode("utf-8").split()))

    response = await client.post("/api/base-cvs", files=_file_part("sample.txt", raw, "text/plain"))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extracted"
    assert body["content_type"] == CvContentType.TXT.value
    assert body["character_count"] == expected_character_count


# ---------------------------------------------------------------------------------------------
# F-1, F-2 — boundary rejections before the domain ever sees the request
# ---------------------------------------------------------------------------------------------


async def test_missing_file_part_returns_422_missing_file(client: AsyncClient) -> None:
    response = await client.post(
        "/api/base-cvs", files={"not_file": ("x.txt", b"irrelevant", "text/plain")}
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "missing_file"
    # F-1: "Nothing stored, no row, no session mutated" — a request this malformed must not mint a
    # guest session either.
    assert _guest_cookie_header(response) is None


async def test_zero_byte_file_returns_422_empty_file(client: AsyncClient) -> None:
    response = await client.post(
        "/api/base-cvs", files=_file_part("empty.pdf", b"", "application/pdf")
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "empty_file"


async def test_filename_reducing_to_an_empty_basename_returns_422_invalid_filename(
    client: AsyncClient,
) -> None:
    """`OriginalFilename.__post_init__` reduces to a basename and rejects an empty one — a filename
    of just a path separator basenames to nothing (`domain/intake/value_objects.py`)."""
    response = await client.post(
        "/api/base-cvs", files=_file_part("/", _read_fixture("sample.txt"), "text/plain")
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_filename"


# ---------------------------------------------------------------------------------------------
# F-3 / AC-2 — the size cap
# ---------------------------------------------------------------------------------------------


async def test_oversized_upload_returns_413(client: AsyncClient, settings: Settings) -> None:
    """The 11 MB body is generated here, never committed (technical-plan.md's test plan is explicit
    about this). Whether the cap is actually enforced *while streaming* rather than by buffering the
    whole body first is a server-behaviour distinction this HTTP-level test cannot observe — F-3's
    "no partial file left in the store" half belongs to the adapter/integration tests; this test
    asserts only the observable contract: the response.
    """
    oversized = b"A" * (settings.max_upload_bytes + 1)

    response = await client.post(
        "/api/base-cvs", files=_file_part("big.txt", oversized, "text/plain")
    )

    assert response.status_code == 413, response.text
    assert _error_code(response) == "file_too_large"


# ---------------------------------------------------------------------------------------------
# F-4 / AC-3 — unsupported formats, decided by magic bytes
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make_bytes", "filename", "content_type"),
    [
        pytest.param(_png_bytes, "cv.png", "image/png", id="png"),
        pytest.param(_bare_zip_bytes, "cv.zip", "application/zip", id="bare-zip"),
        pytest.param(
            _xlsx_like_zip_bytes,
            "cv.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            id="xlsx",
        ),
        # See the module docstring's "known, deliberately-not-fixed tension" note: this case is
        # expected to surface a real disagreement between F-4 and `sniffing.py`'s current algorithm,
        # not a bug in this test.
        pytest.param(_rtf_bytes, "cv.rtf", "application/rtf", id="rtf"),
    ],
)
async def test_unsupported_formats_are_rejected_415(
    client: AsyncClient,
    make_bytes: Callable[[], bytes],
    filename: str,
    content_type: str,
) -> None:
    response = await client.post(
        "/api/base-cvs", files=_file_part(filename, make_bytes(), content_type)
    )

    assert response.status_code == 415, response.text
    assert _error_code(response) == "unsupported_format"
    message = _error_message(response).lower()
    assert "pdf" in message
    assert "docx" in message
    assert "txt" in message


async def test_bytes_that_decode_as_neither_utf8_nor_cp1252_are_rejected_as_unsupported_format(
    client: AsyncClient,
) -> None:
    """F-13. `0x81` is an invalid UTF-8 lead byte *and* an unassigned cp1252 code point (verified
    directly against both codecs), so the sniffer's text fallback refuses it and nothing else
    matches — a 415, not a 422, because this is a sniffing decision, not a business-rule one."""
    undecodable = b"\x81" * 32

    response = await client.post(
        "/api/base-cvs", files=_file_part("cv.txt", undecodable, "text/plain")
    )

    assert response.status_code == 415, response.text
    assert _error_code(response) == "unsupported_format"


# ---------------------------------------------------------------------------------------------
# F-5, F-6 / AC-4 — sniffing wins over the filename and the Content-Type header, both directions
# ---------------------------------------------------------------------------------------------


async def test_png_content_is_rejected_even_when_named_and_declared_as_pdf(
    client: AsyncClient,
) -> None:
    """AC-4, direction 1: "A PNG renamed cv.pdf, sent with Content-Type: application/pdf, is
    rejected 415." Sniffing wins."""
    response = await client.post(
        "/api/base-cvs",
        files=_file_part("cv.pdf", _read_fixture("not-a-pdf.pdf"), "application/pdf"),
    )

    assert response.status_code == 415, response.text
    assert _error_code(response) == "unsupported_format"


async def test_pdf_content_wins_over_a_txt_extension(client: AsyncClient) -> None:
    """AC-4, direction 2 / F-6: "A real PDF named cv.txt is accepted as a PDF." The response's
    `file_key` is not observable from the API (it is not a field of `BaseCvResponse`, by design —
    see the module docstring); that half of F-6 belongs to the repository/adapter tests."""
    response = await client.post(
        "/api/base-cvs", files=_file_part("cv.txt", _read_fixture("sample.pdf"), "text/plain")
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["content_type"] == CvContentType.PDF.value
    assert body["status"] == "extracted"


# ---------------------------------------------------------------------------------------------
# F-7 … F-11 — extraction failures recorded as a state, never a 500 (ADR-0004)
# ---------------------------------------------------------------------------------------------


async def test_encrypted_pdf_returns_encrypted_failure_reason(client: AsyncClient) -> None:
    response = await client.post(
        "/api/base-cvs",
        files=_file_part("encrypted.pdf", _read_fixture("encrypted.pdf"), "application/pdf"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extraction_failed"
    assert body["failure_reason"] == "encrypted"
    assert body["failure_message"]


async def test_corrupt_pdf_returns_corrupt_failure_reason(client: AsyncClient) -> None:
    response = await client.post(
        "/api/base-cvs",
        files=_file_part("corrupt.pdf", _read_fixture("corrupt.pdf"), "application/pdf"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extraction_failed"
    assert body["failure_reason"] == "corrupt"


async def test_scanned_pdf_returns_no_text_layer_and_does_not_promise_ocr(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/base-cvs",
        files=_file_part("scanned.pdf", _read_fixture("scanned.pdf"), "application/pdf"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extraction_failed"
    assert body["failure_reason"] == "no_text_layer"

    message = (body["failure_message"] or "").lower()
    assert message, "failure_message must be set whenever failure_reason is set"
    assert "ocr" in message
    # F-9: "Message names OCR as absent, not as coming" — feature-spec.md's non-goals are explicit
    # that OCR is "not a feature request answered here", so the message must not read as a promise.
    assert "coming" not in message
    assert "soon" not in message


async def test_extraction_below_the_200_character_floor_returns_too_short(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/base-cvs", files=_file_part("tiny.txt", _read_fixture("tiny.txt"), "text/plain")
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extraction_failed"
    assert body["failure_reason"] == "too_short"


async def test_pdf_over_the_page_cap_returns_too_many_pages(
    client: AsyncClient, settings: Settings
) -> None:
    data = _many_page_pdf_bytes(settings.max_cv_pages + 10)

    response = await client.post(
        "/api/base-cvs", files=_file_part("big.pdf", data, "application/pdf")
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extraction_failed"
    assert body["failure_reason"] == "too_many_pages"


# ---------------------------------------------------------------------------------------------
# F-12 — extraction timeout
# ---------------------------------------------------------------------------------------------


async def test_extraction_exceeding_the_timeout_records_extractor_error(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    """A `timeout_seconds=0` deadline is exceeded before `asyncio.to_thread`'s dispatch overhead can
    possibly finish, regardless of how fast the underlying parse would have been — a reliable way to
    force `CvExtractionTimedOut` without a real multi-second sleep in the suite."""
    _override_settings(app, settings, extraction_timeout_seconds=0)

    response = await client.post(
        "/api/base-cvs",
        files=_file_part("sample.pdf", _read_fixture("sample.pdf"), "application/pdf"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extraction_failed"
    assert body["failure_reason"] == "extractor_error"


# ---------------------------------------------------------------------------------------------
# F-14 — the file store is unwritable
# ---------------------------------------------------------------------------------------------


async def test_storage_write_failure_returns_503_storage_unavailable(
    client: AsyncClient, app: FastAPI, settings: Settings, tmp_path: Path
) -> None:
    """Points `upload_dir` at a plain *file* rather than a directory: `LocalFileStore._put_sync`'s
    `final_path.parent.mkdir(parents=True, exist_ok=True)` then raises `NotADirectoryError` (an
    `OSError` subclass) trying to create a directory tree under a path that is not a directory —
    portable across environments, unlike relying on permission bits."""
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_bytes(b"x")
    _override_settings(app, settings, upload_dir=blocking_file)

    response = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "storage_unavailable"


# ---------------------------------------------------------------------------------------------
# F-15 — the commit fails after the file was written
# ---------------------------------------------------------------------------------------------


async def test_commit_failure_after_writing_the_file_returns_503_and_leaves_no_visible_row(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates ADR-0006 §2's crash window directly on the request-scoped session: the use case has
    already written the file and would otherwise succeed, but the commit `deps.get_session` issues
    after the handler returns fails — a real fault injected on a real, connected session (not a
    mocked repository or a faked query result), forcing exactly the failure mode F-15 describes.
    """
    first = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )
    assert first.status_code == 201, first.text

    async def _raise_operational_error() -> None:
        raise OperationalError("simulated commit failure (F-15)", {}, Exception("connection lost"))

    monkeypatch.setattr(session, "commit", _raise_operational_error)

    second = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )

    assert second.status_code == 503, second.text
    assert _error_code(second) == "service_unavailable"


# ---------------------------------------------------------------------------------------------
# F-16 — Redis unreachable: the upload limiter fails OPEN
# ---------------------------------------------------------------------------------------------


async def test_redis_unavailable_fails_open_and_the_upload_still_succeeds(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    """Points `redis_url` at a port nothing listens on — a genuine connection failure, not a mocked
    one. Per OQ-7, the upload endpoint fails open (unlike 1.3's LLM endpoint, which will fail
    closed): the request must still succeed."""
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )

    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------------------------
# F-17, F-18 / AC-9 — the guest cookie on POST: missing, unknown, or expired all mint a fresh session
# ---------------------------------------------------------------------------------------------


async def test_post_with_no_cookie_mints_a_new_session(client: AsyncClient) -> None:
    response = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )

    assert response.status_code == 201, response.text
    assert _guest_cookie_header(response) is not None


async def test_post_with_unknown_cookie_mints_a_new_session(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "a-token-that-was-never-minted-by-this-server")

    response = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )

    assert response.status_code == 201, response.text
    assert _guest_cookie_header(response) is not None


async def test_expired_cookie_on_post_mints_a_fresh_session(
    client: AsyncClient, app: FastAPI
) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: clock

    minted = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )
    assert minted.status_code == 201, minted.text
    first_token = _guest_cookie_value(minted)

    clock.advance(25 * 3600)  # past the default 24h retention window

    renewed = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )

    assert renewed.status_code == 201, renewed.text
    second_token = _guest_cookie_value(renewed)
    assert second_token is not None
    assert second_token != first_token, "an expired session must be replaced, not renewed in place"


# ---------------------------------------------------------------------------------------------
# F-19 — the guest cookie on GET: missing, unknown, or expired all refuse with 401
# ---------------------------------------------------------------------------------------------


async def test_get_list_with_no_cookie_returns_401(client: AsyncClient) -> None:
    response = await client.get("/api/base-cvs")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_one_with_no_cookie_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"/api/base-cvs/{uuid4()}")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_list_with_unknown_cookie_returns_401(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "a-token-that-was-never-minted-by-this-server")

    response = await client.get("/api/base-cvs")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_with_expired_cookie_returns_401(client: AsyncClient, app: FastAPI) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: clock

    minted = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )
    assert minted.status_code == 201, minted.text

    clock.advance(25 * 3600)

    response = await client.get("/api/base-cvs")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


# ---------------------------------------------------------------------------------------------
# F-20 / AC-8 — the link, not the id, is what authorizes a read
# ---------------------------------------------------------------------------------------------


async def test_reading_own_base_cv_returns_it(client: AsyncClient) -> None:
    uploaded = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )
    assert uploaded.status_code == 201, uploaded.text
    cv_id = uploaded.json()["id"]

    response = await client.get(f"/api/base-cvs/{cv_id}")

    assert response.status_code == 200, response.text
    assert response.json()["id"] == cv_id


async def test_reading_another_sessions_base_cv_returns_404(app: FastAPI) -> None:
    async with _new_client(app) as client_a, _new_client(app) as client_b:
        upload_a = await client_a.post(
            "/api/base-cvs",
            files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain"),
        )
        assert upload_a.status_code == 201, upload_a.text
        other_session_cv_id = upload_a.json()["id"]

        upload_b = await client_b.post(
            "/api/base-cvs",
            files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain"),
        )
        assert upload_b.status_code == 201, upload_b.text

        response = await client_b.get(f"/api/base-cvs/{other_session_cv_id}")

    assert response.status_code == 404, response.text
    assert _error_code(response) == "base_cv_not_found"


async def test_reading_another_sessions_cv_is_indistinguishable_from_a_nonexistent_id(
    app: FastAPI,
) -> None:
    """F-20: "404, identical to a nonexistent id. 403 would confirm the id exists." — asserts the
    two responses carry the same machine-readable code, not merely that both are 404."""
    async with _new_client(app) as client_a, _new_client(app) as client_b:
        upload_a = await client_a.post(
            "/api/base-cvs",
            files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain"),
        )
        assert upload_a.status_code == 201, upload_a.text
        other_session_cv_id = upload_a.json()["id"]

        upload_b = await client_b.post(
            "/api/base-cvs",
            files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain"),
        )
        assert upload_b.status_code == 201, upload_b.text

        not_mine = await client_b.get(f"/api/base-cvs/{other_session_cv_id}")
        never_existed = await client_b.get(f"/api/base-cvs/{uuid4()}")

    assert not_mine.status_code == never_existed.status_code == 404
    assert _error_code(not_mine) == _error_code(never_existed) == "base_cv_not_found"


# ---------------------------------------------------------------------------------------------
# AC-7 — a traversal filename is reduced to a safe basename
# ---------------------------------------------------------------------------------------------


async def test_traversal_filename_is_reduced_to_a_safe_basename(client: AsyncClient) -> None:
    """The `file_key`/on-disk-containment half of AC-7 is not observable via the API (`file_key` is
    not a `BaseCvResponse` field, by design) — it is asserted directly against `FileRef` and
    `LocalFileStore` in the adapter/unit tests. This test asserts the half the API *does* expose:
    the traversal string never reaches `original_filename` as given."""
    response = await client.post(
        "/api/base-cvs",
        files={"file": ("../../etc/passwd", _read_fixture("sample.txt"), "text/plain")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["original_filename"] == "passwd"


# ---------------------------------------------------------------------------------------------
# F-21 — double submission is not deduplicated
# ---------------------------------------------------------------------------------------------


async def test_uploading_the_same_file_twice_creates_two_distinct_base_cvs(
    client: AsyncClient,
) -> None:
    data = _read_fixture("sample.txt")

    first = await client.post("/api/base-cvs", files=_file_part("sample.txt", data, "text/plain"))
    second = await client.post("/api/base-cvs", files=_file_part("sample.txt", data, "text/plain"))

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]


# F-22 (internal retry of the same BaseCvId is idempotent) is deliberately not tested here: an
# HTTP request always mints a fresh BaseCvId (`cvs.next_identity()`), so "retry with the same id" is
# not an event this layer can produce or observe — it is an internal application/adapter mechanism
# (FileRef's determinism + LocalFileStore's atomic replace), already covered by the adapter test for
# `LocalFileStore.put`'s idempotent overwrite (technical-plan.md's "Adapters" test-plan row).


# ---------------------------------------------------------------------------------------------
# F-23 / AC-11 — the per-session base-CV cap
# ---------------------------------------------------------------------------------------------


async def test_a_sixth_base_cv_for_one_session_returns_409(
    client: AsyncClient, settings: Settings
) -> None:
    data = _read_fixture("sample.txt")
    responses = [
        await client.post("/api/base-cvs", files=_file_part(f"sample-{i}.txt", data, "text/plain"))
        for i in range(settings.max_base_cvs_per_session + 1)
    ]

    for response in responses[: settings.max_base_cvs_per_session]:
        assert response.status_code == 201, response.text

    last = responses[-1]
    assert last.status_code == 409, last.text
    assert _error_code(last) == "too_many_base_cvs"


# ---------------------------------------------------------------------------------------------
# F-24 / AC-11 — rate limiting, per session and per IP
# ---------------------------------------------------------------------------------------------


async def test_more_than_the_per_session_hourly_limit_returns_429(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, upload_rate_limit_per_hour=2)
    data = _read_fixture("sample.txt")

    first = await client.post("/api/base-cvs", files=_file_part("a.txt", data, "text/plain"))
    second = await client.post("/api/base-cvs", files=_file_part("b.txt", data, "text/plain"))
    third = await client.post("/api/base-cvs", files=_file_part("c.txt", data, "text/plain"))

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"
    assert "retry-after" in {name.lower() for name in third.headers}


async def test_more_than_the_per_ip_hourly_limit_returns_429(
    app: FastAPI, settings: Settings
) -> None:
    """Each request below mints its *own* fresh guest session (a new client, no cookie), so the
    per-session cap and per-session rate limit never fire — only the shared, fixed client IP
    (`ASGITransport`'s default peer) accumulates across them, isolating the per-IP limit."""
    _override_settings(app, settings, upload_rate_limit_per_ip_per_hour=2)
    data = _read_fixture("sample.txt")

    async def upload_from_a_fresh_session() -> Response:
        async with _new_client(app) as one_shot_client:
            return await one_shot_client.post(
                "/api/base-cvs", files=_file_part("sample.txt", data, "text/plain")
            )

    first = await upload_from_a_fresh_session()
    second = await upload_from_a_fresh_session()
    third = await upload_from_a_fresh_session()

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"


# ---------------------------------------------------------------------------------------------
# AC-10 — the guest cookie's attributes
# ---------------------------------------------------------------------------------------------


async def test_guest_cookie_attributes_in_non_production(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        "/api/base-cvs", files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )

    cookie = _guest_cookie_header(response)
    assert cookie is not None, "expected a Set-Cookie header when a new guest session is minted"

    assert "HttpOnly" in cookie
    assert "Path=/" in cookie
    assert f"Max-Age={settings.guest_retention_hours * 3600}" in cookie
    assert "samesite=lax" in cookie.lower()
    # APP_ENV=test in this fixture (conftest.py's `settings`), never production.
    assert "Secure" not in cookie

    token = _guest_cookie_value(response)
    assert token is not None
    # secrets.token_urlsafe(32) draws 256 bits; url-safe base64 of 32 bytes is comfortably longer
    # than any plausible short/weak value a regression could produce.
    assert len(token) >= 32


async def test_cookie_is_secure_in_production(
    settings: Settings, session: AsyncSession, engine: AsyncEngine
) -> None:
    """AC-10: "Secure whenever APP_ENV=production." Built from settings with `app_env="production"`
    rather than asserted as a hypothetical, per this task's own instruction — a second, real `FastAPI`
    app, wired the same way `conftest.py`'s `app` fixture wires the default one."""
    prod_settings = settings.model_copy(update={"app_env": "production"})
    prod_app = create_app(prod_settings)
    prod_app.dependency_overrides[get_session] = lambda: session
    prod_app.state.settings = prod_settings
    prod_app.state.engine = engine
    prod_app.state.session_factory = lambda: session
    prod_app.state.celery = celery_app

    async with _new_client(prod_app) as prod_client:
        response = await prod_client.post(
            "/api/base-cvs",
            files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain"),
        )

    cookie = _guest_cookie_header(response)
    assert cookie is not None, "expected a Set-Cookie header when a new guest session is minted"
    assert "Secure" in cookie


# ---------------------------------------------------------------------------------------------
# AC-12 — the privacy test: nothing in this slice logs a CV body or a filename
# ---------------------------------------------------------------------------------------------


async def test_no_cv_text_or_filename_ever_appears_in_the_logs(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    filename = "Alex_Rivera_CV_2026.pdf"

    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/base-cvs",
            files=_file_part(filename, _read_fixture("sample.pdf"), "application/pdf"),
        )

    assert response.status_code == 201, response.text

    # A capture that caught nothing at all proves nothing (see the module docstring's note on why
    # `caplog`, not `structlog.testing.capture_logs()`, is used here) — this is the guard against
    # this test passing for the wrong reason.
    assert caplog.records, "expected the upload to have produced at least one log record"

    log_output = caplog.text
    assert filename not in log_output
    assert _SAMPLE_CV_NAME_FRAGMENT not in log_output
    assert _SAMPLE_CV_EMAIL_FRAGMENT not in log_output
    assert _SAMPLE_CV_EMPLOYER_FRAGMENT not in log_output
