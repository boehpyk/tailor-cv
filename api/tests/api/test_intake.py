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
import json
import logging
import random
import zipfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pypdf import PdfWriter
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.types import Message, Scope

from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, ExtractionFailureReason
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.api.deps import get_app_settings, get_clock, get_session
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.api.routers.intake import _failure_message
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


def _corrupt_bytes(data: bytes, seed: int, n: int) -> bytes:
    """Flip `n` random bytes of `data` under a fixed `seed` — deterministic across runs and
    machines, which is what lets the specific findings below be reproduced byte-for-byte rather
    than "some corrupted variant or other" (the sweep tests that found them live in
    `tests/integration/adapters/test_extraction.py` and `test_sniffing.py`)."""
    rng = random.Random(seed)  # noqa: S311 — deterministic corruption, not cryptography
    mutable = bytearray(data)
    for position in rng.sample(range(len(mutable)), n):
        mutable[position] = rng.randrange(256)
    return bytes(mutable)


def _corrupt_tail_bytes(data: bytes, seed: int, n: int, tail: int) -> bytes:
    """Same as `_corrupt_bytes`, but confined to the last `tail` bytes — where a zip's central
    directory and End Of Central Directory record live (see `test_sniffing.py`'s `_corrupt_tail`
    for why that concentration matters)."""
    rng = random.Random(seed)  # noqa: S311 — deterministic corruption, not cryptography
    mutable = bytearray(data)
    start = max(0, len(mutable) - tail)
    for position in rng.sample(range(start, len(mutable)), min(n, len(mutable) - start)):
        mutable[position] = rng.randrange(256)
    return bytes(mutable)


def _malformed_docx_bytes() -> bytes:
    """`sample.docx` with `word/document.xml` replaced by well-formed XML that has no `<w:body>`
    element — a *valid* zip that python-docx cannot read: `.paragraphs` does
    `document.element.body.p_lst`, and `body` is `None`, raising a bare `AttributeError` (verified
    empirically). F-8/F-15's end-to-end case: this must not be a 500 with the file on disk and no
    row (ADR-0004)."""
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
    return buffer.getvalue()


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


async def test_middleware_rejects_a_spoofed_content_length_even_with_a_tiny_actual_body(
    client: AsyncClient, settings: Settings
) -> None:
    """AC-2/F-3, corrected 2026-09-08: `MaxBodySizeMiddleware` decides from the declared
    `Content-Length` header alone, never from how many bytes actually arrive. Proven by an asymmetry
    `_read_capped`'s counting loop could never produce: a `Content-Length` far above the cap, paired
    with a body that is genuinely tiny. If this 413 came from counting bytes as they were read off an
    already-spooled body, it could not have fired — there were never enough bytes on the wire to
    count past the cap in the first place.

    `httpx.Request`/`client.build_request` is used directly (not `files=`) so the declared header and
    the real body can disagree; `httpx.ASGITransport.handle_async_request` forwards `request.headers`
    into `scope["headers"]` verbatim and streams only the real body through `receive()` (read from
    `httpx`'s own source before relying on it here), so this really is the asymmetry the fix depends
    on, not an artifact of how the test builds the request.
    """
    spoofed_length = settings.max_upload_bytes + 1_000_000
    request = client.build_request(
        "POST",
        "/api/base-cvs",
        content=b"tiny",
        # Multipart, because this asserts the UPLOAD cap. Slice 1.2 gave the middleware a second,
        # much smaller cap for non-multipart bodies (256 KiB, `request_too_large`), so the
        # content-type now decides which 413 applies — `text/plain` here would take the JSON branch
        # and stop testing what this test is named for.
        headers={
            "content-length": str(spoofed_length),
            "content-type": "multipart/form-data; boundary=----tc",
        },
    )

    response = await client.send(request)

    assert response.status_code == 413, response.text
    # Byte-for-byte the same envelope a router-level 413 emits for the identical condition
    # (`routers/intake.py::_read_capped` raises `HTTPException` with this exact `code` and the same
    # f-string over the same `settings.max_upload_bytes`) — a client branching on `error.code` must
    # never be able to tell which of the two layers caught it.
    assert response.json() == {
        "error": {
            "code": "file_too_large",
            "message": f"The file exceeds the {settings.max_upload_bytes}-byte limit.",
        }
    }


async def test_middleware_never_calls_receive_on_the_rejection_path(
    app: FastAPI, settings: Settings
) -> None:
    """The mechanism the whole fix rests on (`middleware.py`'s docstring): never draining the ASGI
    receive channel is what leaves an `Expect: 100-continue` client still waiting for permission to
    send its body, so a compliant client never transmits it at all. Asserted directly, by driving the
    ASGI app itself — past `httpx`/`ASGITransport` entirely — with a `receive` that raises if it is
    ever awaited. A 413 coming back with `receive` untouched is the only way this test can pass.
    """
    spoofed_length = settings.max_upload_bytes + 1_000_000

    async def _receive_must_not_be_called() -> Message:
        raise AssertionError("MaxBodySizeMiddleware must reject before ever calling receive()")

    sent: list[Message] = []

    async def _send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/base-cvs",
        "raw_path": b"/api/base-cvs",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-length", str(spoofed_length).encode("ascii")),
            (b"content-type", b"multipart/form-data; boundary=xyz"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    await app(scope, _receive_must_not_be_called, _send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = next(message for message in sent if message["type"] == "http.response.body")
    assert start["status"] == 413
    assert json.loads(body["body"])["error"]["code"] == "file_too_large"


async def test_the_413_is_readable_cross_origin_because_max_body_size_sits_inside_cors(
    settings: Settings,
) -> None:
    """Pins `main.py`'s ordering invariant, corrected 2026-09-08: `MaxBodySizeMiddleware` is
    registered *before* `CORSMiddleware`, which — because Starlette's `add_middleware` prepends and
    `build_middleware_stack` wraps over `reversed(...)` — makes the LAST-registered middleware
    OUTERMOST, so `MaxBodySizeMiddleware` ends up *inside* CORS, not outside it:

        ServerErrorMiddleware > CORSMiddleware > MaxBodySizeMiddleware > ExceptionMiddleware > router

    That is what lets a cross-origin browser actually *read* the 413: `CORSMiddleware` only ever
    inspects and stamps `Access-Control-Allow-Origin` on a response passing through it, regardless of
    which inner layer produced that response, so a 413 raised inside it still comes back carrying the
    header. The `access-control-allow-origin` assertion below is the one that fails if someone "fixes"
    the ordering by moving `MaxBodySizeMiddleware`'s registration to *after* the CORS block, making it
    outermost: the 413 would then leave the app before ever passing through `CORSMiddleware`, arrive
    at the browser with no CORS headers at all, and a cross-origin `fetch` would reject with an opaque
    network error — telling the user nothing about a file we know exactly what is wrong with.

    Builds its own app rather than using the shared `app`/`client` fixtures: those are wired from the
    session-scoped `settings` fixture, whose `cors_origins` is `""` (CORS middleware not even
    registered) — mutating it here would leak into every other test in this module.
    """
    cors_settings = settings.model_copy(update={"cors_origins": "http://example.test"})
    cors_app = create_app(cors_settings)
    spoofed_length = cors_settings.max_upload_bytes + 1_000_000

    async with _new_client(cors_app) as cors_client:
        request = cors_client.build_request(
            "POST",
            "/api/base-cvs",
            content=b"tiny",
            headers={
                "content-length": str(spoofed_length),
                # Declares multipart because this asserts the UPLOAD cap. Slice 1.2 gave
                # `MaxBodySizeMiddleware` a second, much smaller cap for non-multipart bodies
                # (`json_request_max_bytes`, 256 KiB, answering `request_too_large`), so the
                # content-type now decides WHICH 413 a request gets. Before that there was one cap
                # and this header was arbitrary; now sending `text/plain` here would exercise the
                # JSON path and quietly stop testing the thing this test is named for.
                "content-type": "multipart/form-data; boundary=----tc",
                "origin": "http://example.test",
            },
        )
        response = await cors_client.send(request)

    assert response.status_code == 413, response.text
    assert _error_code(response) == "file_too_large"
    assert response.headers.get("access-control-allow-origin") == "http://example.test"


async def test_middleware_never_calls_receive_on_the_rejection_path_with_cors_enabled(
    settings: Settings,
) -> None:
    """The receive-transparency property `test_middleware_never_calls_receive_on_the_rejection_path`
    proves above must still hold once `CORSMiddleware` sits outside `MaxBodySizeMiddleware` in the
    real stack — this is not a lucky accident of that test's CORS-disabled configuration. `main.py`'s
    comment states the mechanism: `CORSMiddleware` wraps `send`, never `receive`, so it is
    receive-transparent and cannot itself drain the channel this whole check depends on staying
    undrained. Driving the ASGI app directly (past `httpx`/`ASGITransport`) with a `receive` that
    raises if ever awaited is what actually exercises that: a 413 coming back — through CORS —
    with `receive` untouched is the only way this test can pass.
    """
    cors_settings = settings.model_copy(update={"cors_origins": "http://example.test"})
    cors_app = create_app(cors_settings)
    spoofed_length = cors_settings.max_upload_bytes + 1_000_000

    async def _receive_must_not_be_called() -> Message:
        raise AssertionError("MaxBodySizeMiddleware must reject before ever calling receive()")

    sent: list[Message] = []

    async def _send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/base-cvs",
        "raw_path": b"/api/base-cvs",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-length", str(spoofed_length).encode("ascii")),
            (b"content-type", b"multipart/form-data; boundary=xyz"),
            (b"origin", b"http://example.test"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    await cors_app(scope, _receive_must_not_be_called, _send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = next(message for message in sent if message["type"] == "http.response.body")
    assert start["status"] == 413
    assert json.loads(body["body"])["error"]["code"] == "file_too_large"
    header_names = {name.lower() for name, _value in start["headers"]}
    assert b"access-control-allow-origin" in header_names


async def test_health_endpoints_are_exempt_from_the_body_size_check(
    client: AsyncClient, settings: Settings
) -> None:
    """`MaxBodySizeMiddleware.exempt_prefixes` includes `/health` so a cheap, bodyless, frequently
    polled route never pays for a `Content-Length` check it can never fail (`middleware.py`). Proven
    with a `Content-Length` well above the cap — the same value that gets a 413 on every other
    route in this file — sent to a route this middleware is supposed to leave alone."""
    spoofed_length = settings.max_upload_bytes + 1_000_000

    response = await client.get("/health/live", headers={"content-length": str(spoofed_length)})

    assert response.status_code == 200, response.text


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


async def test_a_corrupted_zip_with_a_broken_central_directory_is_rejected_415_not_500(
    client: AsyncClient,
) -> None:
    """`sniffing.py::_is_docx` used to catch only `zipfile.BadZipFile`. A corrupted zip central
    directory can instead raise `struct.error`, `ValueError`, `EOFError`, `OverflowError` or
    `zipfile.LargeZipFile`, and every one of those escaped `_is_docx`, escaped
    `sniff_cv_content_type`, and became a bare 500 for a file whose only real problem is not being a
    readable DOCX — which is a 415. This exact byte sequence (`sample.docx` with 10 bytes flipped
    under a fixed seed, confined to the last 300 bytes where the central directory and EOCD record
    live) raises `NotImplementedError('zip file version 17.2')` from `zipfile.ZipFile.__init__` —
    verified empirically, and not `BadZipFile` — so it stands in for the sweep in
    `tests/integration/adapters/test_sniffing.py` as the one deterministic case proven end-to-end.
    """
    corrupted = _corrupt_tail_bytes(_read_fixture("sample.docx"), seed=56, n=10, tail=300)
    assert corrupted.startswith(b"PK\x03\x04"), (
        "expected the corruption to leave the zip magic intact"
    )

    response = await client.post(
        "/api/base-cvs",
        files=_file_part(
            "cv.docx",
            corrupted,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
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
# F-8 / F-15 — the extraction catch-all's own regression: a library exception with no allow-list
# entry must still be a recorded state, never a 500 with the file on disk and no row (ADR-0004).
# ---------------------------------------------------------------------------------------------


async def test_a_docx_with_a_malformed_document_xml_is_recorded_as_extractor_error(
    client: AsyncClient, settings: Settings
) -> None:
    """A valid zip whose `word/document.xml` is well-formed XML but has no `<w:body>` element used
    to raise a bare `AttributeError` out of python-docx's `.paragraphs` property — before
    `PypdfDocxTextExtractor` grew its catch-all (`extraction.py`'s `except Exception` clause), this
    escaped as an uncaught 500 with the file already written and no row (verified empirically; the
    adapter-level proof lives in `tests/integration/adapters/test_extraction.py`). Now it is a
    recorded state: 201, `extraction_failed`/`extractor_error`, the row is readable, and the file
    really is on disk — the two halves of ADR-0004's "a failed run is a recorded state, never a 500
    with nothing on disk."""
    response = await client.post(
        "/api/base-cvs",
        files=_file_part(
            "cv.docx",
            _malformed_docx_bytes(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extraction_failed"
    assert body["failure_reason"] == "extractor_error"

    # The row exists and is readable back through the same session.
    get_response = await client.get(f"/api/base-cvs/{body['id']}")
    assert get_response.status_code == 200, get_response.text

    # The file really is on disk, not merely claimed to be — `FileRef` is deterministic from the id
    # and content type (ADR-0011), so the expected key can be computed rather than guessed.
    ref = FileRef.for_base_cv(BaseCvId(UUID(body["id"])), CvContentType(body["content_type"]))
    assert (settings.upload_dir / ref.key).exists()


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


@pytest.mark.parametrize(
    "fixture_name",
    [
        pytest.param("corrupt.pdf", id="corrupt.pdf"),
        pytest.param("scanned.pdf", id="scanned.pdf"),
    ],
)
async def test_no_cv_text_ever_appears_in_the_logs_for_a_damaged_or_scanned_upload(
    client: AsyncClient, caplog: pytest.LogCaptureFixture, fixture_name: str
) -> None:
    """The finding behind this test: the privacy test above only ever uploads a *clean* PDF, so it
    never exercises the one path `PypdfDocxTextExtractor`'s non-strict parsing takes through a
    damaged file — the path where `pypdf`'s own logger, not this codebase's, has something to say
    (`observability.py`'s `_SILENCED_VENDOR_LOGGERS`). `corrupt.pdf` and `scanned.pdf` are both
    genuinely damaged/content-free PDFs already in the fixture corpus, so this is the same assertion
    against inputs that actually walk that code path — not proof the guard does anything on its own
    (that is `test_the_pypdf_logger_guard_actually_prevents_a_real_content_leak` below), just proof
    that a real upload of a damaged file stays clean with the guard in its normal, active state."""
    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/base-cvs",
            files=_file_part(fixture_name, _read_fixture(fixture_name), "application/pdf"),
        )

    assert response.status_code == 201, response.text
    assert caplog.records, "expected the upload to have produced at least one log record"

    log_output = caplog.text
    assert _SAMPLE_CV_NAME_FRAGMENT not in log_output
    assert _SAMPLE_CV_EMAIL_FRAGMENT not in log_output
    assert _SAMPLE_CV_EMPLOYER_FRAGMENT not in log_output


def _currently_disabled_pypdf_loggers() -> list[logging.Logger]:
    """Every already-instantiated `pypdf*` logger whose `.disabled` flag is set.

    Unrelated to `observability.py`'s guard, and worth naming precisely because it is a second,
    *accidental* reason `pypdf`'s loggers are quiet in this suite: `alembic/env.py`'s generated
    boilerplate calls `logging.config.fileConfig(config.config_file_name)` (from the `_migrated`
    session fixture's `command.upgrade`), and stdlib `fileConfig` defaults
    `disable_existing_loggers=True` — which sets `.disabled = True` on *every* logger that already
    existed at that moment (verified empirically: the disabled set spans `pypdf`, `celery`, `httpx`,
    `sqlalchemy`, `sentry_sdk` and more, none of which `alembic.ini` even mentions), for the rest of
    the test session. A logger's own `.disabled` flag short-circuits `isEnabledFor` before its level
    is even consulted, so this test's whole premise — that undoing `observability.py`'s *level*
    reveals a real leak — would be silently vacuous unless this second, incidental mechanism is also
    neutralised first."""
    return [
        obj
        for name, obj in logging.Logger.manager.loggerDict.items()
        if isinstance(obj, logging.Logger)
        and obj.disabled
        and (name == "pypdf" or name.startswith("pypdf."))
    ]


async def test_the_pypdf_logger_guard_actually_prevents_a_real_content_leak(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Proves the privacy tests above are not vacuous — the finding the reviewer raised. `pypdf`'s
    default `strict=False` parsing catches many internal errors and logs them via its own
    `logging.Logger` (`pypdf.generic._data_structures`, among others) rather than raising, and at
    least one of those call sites logs `repr(exc)` verbatim, which can quote raw document bytes.

    Corrupting `sample.pdf` (5 bytes flipped under a fixed seed) makes that logger emit
    `PdfReadError("Invalid Elementary Object starting with ...(Alex Rivera) Tj E'")` at WARNING —
    the candidate's own name, mid-log-line, from a library this codebase does not control.
    `observability.py`'s `_SILENCED_VENDOR_LOGGERS` is what stands between that and the application's
    own log stream (`configure_logging`, run once per `app` fixture via `create_app`).

    It sweeps a range of seeds and stops at the first that leaks, rather than pinning the one seed
    that was found by hand. See the comment on the sweep for why: the leak depends on pypdf's
    internal call sites, so a single seed is one instance of a class, and the failure message
    distinguishes "pypdf changed" from "something else is suppressing it".

    This test temporarily undoes *both* silencing mechanisms — the deliberate one
    (`observability.py`'s level) and the incidental one this test discovered while being written
    (`_currently_disabled_pypdf_loggers`'s docstring) — to show the leak is real without either, then
    restores both and confirms the identical upload is clean with them back in place. Undoing only
    the level would have made this test pass for the wrong reason: green whether or not
    `observability.py`'s guard does anything at all.

    The incidental mechanism has since been fixed at its source (`alembic/env.py` now passes
    `disable_existing_loggers=False`), so `_currently_disabled_pypdf_loggers()` is expected to
    return an empty list and its loop to be a no-op. It stays anyway: it costs nothing, and it is
    what keeps this test honest if any future fixture disables those loggers again.
    """
    pypdf_logger = logging.getLogger("pypdf")
    guarded_level = pypdf_logger.level
    assert guarded_level > logging.WARNING, (
        "expected configure_logging() (run by the app fixture via create_app) to have already "
        "silenced the pypdf logger above WARNING — if this fails, the guard itself did not apply "
        "and the rest of this test cannot prove anything"
    )
    incidentally_disabled = _currently_disabled_pypdf_loggers()

    # A SWEEP rather than one hand-picked seed, for the same reason the extraction and sniffing
    # tests sweep: one seed proves one instance, and what needs guarding is the class. The
    # discrimination here rests on pypdf's own log call sites and message text — vendor
    # implementation detail with no stability contract — so an upgrade that stops `repr`-ing the
    # offending object would retire whichever single seed we had pinned. A sweep degrades into
    # "find a different leaking variant" instead of "the one seed stopped reproducing".
    leaked_seeds: list[int] = []
    corrupted = b""
    try:
        pypdf_logger.setLevel(logging.WARNING)  # undo observability.py's guard, on purpose
        for logger in incidentally_disabled:
            logger.disabled = False  # undo alembic's fileConfig side effect too, on purpose

        for seed in range(12440, 12464):
            variant = _corrupt_bytes(_read_fixture("sample.pdf"), seed=seed, n=5)
            caplog.clear()
            with caplog.at_level(logging.INFO):
                response = await client.post(
                    "/api/base-cvs", files=_file_part("cv.pdf", variant, "application/pdf")
                )
            assert response.status_code == 201, response.text
            if _SAMPLE_CV_NAME_FRAGMENT in caplog.text:
                leaked_seeds.append(seed)
                corrupted = variant
                break

        assert leaked_seeds, (
            "no corrupted variant leaked CV text through pypdf's own logger with BOTH silencing "
            "mechanisms undone. Either pypdf no longer logs `repr(exc)` at these call sites (in "
            "which case this guard-proof needs a new leak vector, not a wider sweep), or something "
            "else is now suppressing it — and until that is resolved, the AC-12 privacy tests "
            "above cannot be assumed to prove anything"
        )
    finally:
        pypdf_logger.setLevel(guarded_level)
        for logger in incidentally_disabled:
            logger.disabled = True

    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/base-cvs", files=_file_part("cv.pdf", corrupted, "application/pdf")
        )
    assert response.status_code == 201, response.text
    assert _SAMPLE_CV_NAME_FRAGMENT not in caplog.text


# ---------------------------------------------------------------------------------------------
# `_failure_message(EXTRACTOR_ERROR)` — the wording changed to name both causes it now covers
# (the 10 s timeout and the extraction catch-all); AC-15 needs every reason's message distinct.
# ---------------------------------------------------------------------------------------------


def test_failure_message_extractor_error_names_both_the_timeout_and_the_catch_all(
    settings: Settings,
) -> None:
    """The old wording ("we couldn't read this file in time") named only the timeout. Since
    `PypdfDocxTextExtractor` grew its catch-all, `EXTRACTOR_ERROR` is reached by two different
    causes, and this asserts the new sentence actually speaks to both rather than reverting to a
    generic "something went wrong" that would name neither."""
    message = _failure_message(ExtractionFailureReason.EXTRACTOR_ERROR, settings)

    assert "took too long" in message
    assert "gave up part-way" in message
    # The wording this replaced — pinned so a future change is a deliberate decision, not a drift
    # nobody notices (CLAUDE.md: "fix one of them on purpose and say which won").
    assert message != (
        "We couldn't read this file in time. Try again, or use a different PDF, DOCX or TXT file."
    )


def test_failure_message_is_textually_distinct_for_every_extraction_failure_reason(
    settings: Settings,
) -> None:
    """AC-15 needs three textually distinct error *kinds* at the UI layer; this is the narrower,
    machine-checkable claim underneath it at the API layer — the server-owned mapping
    (`_failure_message`'s own docstring: "the client never re-implements this mapping") must never
    hand two different `ExtractionFailureReason` members the same sentence, which is what would make
    them indistinguishable to a user no matter how carefully the frontend renders them."""
    messages = [_failure_message(reason, settings) for reason in ExtractionFailureReason]

    assert len(messages) == len(set(messages)), messages
