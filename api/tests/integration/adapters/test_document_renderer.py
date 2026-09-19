"""Adapter tests for `MarkdownDocumentRenderer` (`DocumentRendererPort`) — written **after** (I10t),
against the real `weasyprint` / `python-docx` / `markdown-it-py` / `nh3` pipeline landed in I9's DOCX
walker (`52c3c69`) and I10's facade (`a37111b`). ADR-0017, AC-30, AC-31, AC-32, AC-34, AC-46, AC-47.

**What is NOT here.** AC-28 (the parse gate), AC-29 (`nh3` on the allow-list) and AC-30(a)
(`refuse_every_url`'s five inputs, `STYLESHEET`'s grep) already have their own pure, no-I/O tests —
`tests/unit/export/test_tokens.py`, `test_html.py`, `test_pdf_constants.py` (I2). This file is the
one level up: the **adapter** wired to the real vendor libraries, run in a thread, under a timeout,
behind the `except Exception` floor.

**No assertion here is written by running the renderer and recording its bytes.** The PDF
assertions are on extracted text and structure (`pypdf`), never on a byte string; the DOCX
assertions are on `python-docx`'s own paragraph/style model. Every expected value below comes from
ADR-0017 §4's mapping (heading -> `Heading N`, bullet -> `List Bullet`, link -> `text (url)`, ...)
or from the spec's failure contract, decided before the renderer was ever called.

**No test in this file opens a real socket.** AC-30(c) proves the opposite: WeasyPrint is handed
hostile HTML with an `<img>` and a `<link rel=stylesheet>`, `socket.socket` is patched to raise on
any construction, and the render still succeeds — because the fetcher refuses before a name is ever
resolved, not because a socket call happened to fail.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import logging
import socket
import time
from pathlib import Path
from typing import Any, NoReturn

import pytest
from docx import Document as read_docx  # the vendor reader, only ever used here to verify output
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from tailorcraft.domain.export.errors import (
    DocumentRenderError,
    DocumentRenderOutputTooLarge,
    DocumentRenderTimedOut,
)
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export import renderer as renderer_module
from tailorcraft.infrastructure.export.html import sanitize_html
from tailorcraft.infrastructure.export.pdf import UrlFetcher, refuse_every_url, render_pdf
from tailorcraft.infrastructure.export.renderer import MarkdownDocumentRenderer
from tailorcraft.infrastructure.settings import Settings
from tests.fixtures.documents import (
    MARKDOWN_FIXTURE_CORPUS,
    MODEL_CV_FIXTURE_MARKDOWN,
    NORMALIZATION_FIXTURE_MARKDOWN,
    MarkdownFixture,
)

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "tailorcraft"
_EXPORT_MODULE_DIR = _SRC_ROOT / "infrastructure" / "export"


def _json_log_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Parse every captured record that is a JSON object, as `test_extraction.py` does."""
    events: list[dict[str, object]] = []
    for record in caplog.records:
        try:
            events.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            continue
    return events


# --- AC-47: the fixture corpus renders to all four formats, no failures -------------------------


@pytest.mark.parametrize(
    "fixture", MARKDOWN_FIXTURE_CORPUS, ids=[f.name for f in MARKDOWN_FIXTURE_CORPUS]
)
@pytest.mark.parametrize("format", list(ExportFormat), ids=[f.value for f in ExportFormat])
async def test_every_corpus_fixture_renders_to_every_format_without_failing(
    settings: Settings, fixture: MarkdownFixture, format: ExportFormat
) -> None:
    """The pre-production floor (AC-47): 100% success across the editor's own corpus x the four
    formats. A failure here means the server's grammar and the editor's grammar have drifted apart
    — the whole reason the two corpora are typed out separately rather than shared."""
    renderer = MarkdownDocumentRenderer(settings)

    rendered = await renderer.render(
        fixture.markdown, document=TailoredDocumentKind.CV, format=format
    )

    assert isinstance(rendered, bytes)
    assert len(rendered) > 0


# --- AC-31: the DOCX round trip through python-docx -----------------------------------------------


async def test_docx_round_trip_maps_headings_bold_italic_bullets_and_links(
    settings: Settings,
) -> None:
    """ADR-0017 §4's mapping, read back with `python-docx`: heading -> `Heading N`, a bullet list
    item -> `List Bullet`, bold/italic -> run flags (invisible in `.text`, so asserted on the runs),
    and a link -> `text (url)`, never a real hyperlink."""
    renderer = MarkdownDocumentRenderer(settings)

    rendered = await renderer.render(
        MODEL_CV_FIXTURE_MARKDOWN, document=TailoredDocumentKind.CV, format=ExportFormat.DOCX
    )

    document = read_docx(io.BytesIO(rendered))
    paragraphs = document.paragraphs

    # The source opens with an H1 ("# Jordan Rivera"), so no synthetic title is added (fact 8).
    assert paragraphs[0].text == "Jordan Rivera"
    assert _style_name(paragraphs[0]) == "Heading 1"

    experience = _paragraph_by_text(paragraphs, "Experience")
    assert _style_name(experience) == "Heading 2"

    role_paragraph = _paragraph_by_text(paragraphs, "Senior Backend Engineer, Acme Corp")
    bold_runs = [r for r in role_paragraph.runs if r.text == "Senior Backend Engineer"]
    assert bold_runs, [r.text for r in role_paragraph.runs]
    assert bold_runs[0].bold is True

    bullet_paragraph = _paragraph_by_text(
        paragraphs, "Led the migration to a hexagonal architecture"
    )
    assert _style_name(bullet_paragraph) == "List Bullet"
    italic_runs = [r for r in bullet_paragraph.runs if r.text == "hexagonal"]
    assert italic_runs, [r.text for r in bullet_paragraph.runs]
    assert italic_runs[0].italic is True

    education = _paragraph_by_text(paragraphs, "Education")
    assert _style_name(education) == "Heading 2"

    degree = _paragraph_by_text(paragraphs, "BSc Computer Science")
    assert _style_name(degree) == "Heading 3"

    # ADR-0017 §4: no hyperlink API used, the URL is printed as plain text after the label.
    link_paragraph = _paragraph_by_text(paragraphs, "University website (https://example.edu)")
    assert _style_name(link_paragraph) in ("Normal", None)


async def test_docx_adds_a_synthetic_title_only_when_the_source_has_no_h1(
    settings: Settings,
) -> None:
    """Fact 8: the DOCX title paragraph ("Tailored CV" / "Cover Letter") is added only when the
    author's own document does not open with an H1 — most real fixtures have one and must NOT get
    it, so this covers the other branch explicitly."""
    renderer = MarkdownDocumentRenderer(settings)

    rendered = await renderer.render(
        NORMALIZATION_FIXTURE_MARKDOWN, document=TailoredDocumentKind.CV, format=ExportFormat.DOCX
    )

    document = read_docx(io.BytesIO(rendered))
    assert document.paragraphs[0].text == "Tailored CV"
    assert _style_name(document.paragraphs[0]) == "Heading 1"

    rendered_letter = await renderer.render(
        NORMALIZATION_FIXTURE_MARKDOWN,
        document=TailoredDocumentKind.COVER_LETTER,
        format=ExportFormat.DOCX,
    )
    letter_document = read_docx(io.BytesIO(rendered_letter))
    assert letter_document.paragraphs[0].text == "Cover Letter"


def _paragraph_by_text(paragraphs: list[Paragraph], text: str) -> Paragraph:
    matches = [p for p in paragraphs if p.text == text]
    assert matches, f"no paragraph with text {text!r}; had {[p.text for p in paragraphs]}"
    return matches[0]


def _style_name(paragraph: Paragraph) -> str | None:
    """`paragraph.style` is `ParagraphStyle | None` per the vendor's own types; `Normal` (no
    explicit style) is a legitimate, asserted-for outcome here (the link paragraph), not a bug."""
    style = paragraph.style
    return style.name if style is not None else None


# --- AC-31: the import-graph assertion -------------------------------------------------------------


def test_no_module_in_export_imports_both_vendor_docx_and_our_html_module() -> None:
    """`infrastructure/export/` must have no module that imports **the vendor** `python-docx`
    package AND our own `html.py` (ADR-0017 §4). A facade (`renderer.py`) is allowed to import
    *our* `export.docx` walker and `export.html` at once — that is the whole point of a facade —
    so this must key off the vendor package's own dotted name, never the substring `docx`."""
    offenders: list[str] = []
    imports_vendor_docx_by_module: dict[str, bool] = {}
    imports_html_module_by_module: dict[str, bool] = {}

    for path in sorted(_EXPORT_MODULE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        imports_vendor_docx = False
        imports_html_module = False

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "docx" or alias.name.startswith("docx."):
                        imports_vendor_docx = True
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "docx" or module.startswith("docx."):
                    imports_vendor_docx = True
                if module == "tailorcraft.infrastructure.export.html":
                    imports_html_module = True
                # A relative `from . import html` inside this very package.
                if (
                    node.level > 0
                    and module in ("", "export")
                    and any(a.name == "html" for a in node.names)
                ):
                    imports_html_module = True

        imports_vendor_docx_by_module[path.name] = imports_vendor_docx
        imports_html_module_by_module[path.name] = imports_html_module
        if imports_vendor_docx and imports_html_module:
            offenders.append(path.name)

    assert offenders == [], f"modules importing both vendor docx and html.py: {offenders}"

    # Positive proof the assertion above is not vacuous: docx.py really does import the vendor
    # package, html.py really is importable-by-name, and docx.py does not import it.
    assert imports_vendor_docx_by_module.get("docx.py") is True
    assert imports_html_module_by_module.get("docx.py") is False


# --- AC-46: a real PDF, a real font — slow ----------------------------------------------------------


@pytest.mark.slow
async def test_pdf_embeds_a_font_and_page_one_contains_the_heading_and_the_name(
    settings: Settings,
) -> None:
    """AC-46: rendered by the REAL WeasyPrint, read back by `pypdf`. A page of boxes fails the text
    assertion (no font resolved, so nothing recognisable extracts); a missing font fails the embed
    assertion. Neither is inferred — both are measured on the produced PDF."""
    renderer = MarkdownDocumentRenderer(settings)

    rendered = await renderer.render(
        MODEL_CV_FIXTURE_MARKDOWN, document=TailoredDocumentKind.CV, format=ExportFormat.PDF
    )

    reader = PdfReader(io.BytesIO(rendered))
    assert len(reader.pages) >= 1
    page_one_text = reader.pages[0].extract_text()
    assert "Jordan Rivera" in page_one_text
    assert "Experience" in page_one_text

    assert _pdf_has_embedded_font_file(reader), "expected at least one /FontFile in the PDF"


def _pdf_has_embedded_font_file(reader: PdfReader) -> bool:
    """WeasyPrint subsets and embeds every font it uses as a **Type0 composite font**: the page's
    `/Font` entry carries no `/FontDescriptor` of its own — it carries `/DescendantFonts`, a
    CIDFontType2 dictionary one level down, and *that* is where `/FontDescriptor` and the actual
    `/FontFile2` bytes live. Measured against the real output above, not assumed from the PDF spec's
    simple-font shape.

    `Any` below, with this comment as the justification (CLAUDE.md): a PDF's own object graph is a
    dynamically-typed dict-of-dicts by the format's own design (a `/Font` entry can be a simple font
    or a Type0 composite, a `/Resources` dict may or may not carry a `/Font` key at all), and pypdf's
    own types reflect that rather than something this test could usefully narrow further.
    """
    for page in reader.pages:
        resources: Any = page.get("/Resources")
        if resources is None:
            continue
        fonts = resources.get("/Font")
        if fonts is None:
            continue
        for font_ref in fonts.values():
            font = font_ref.get_object()
            for descriptor in _font_descriptors(font):
                if any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                    return True
    return False


def _font_descriptors(font: Any) -> list[Any]:
    """Every `/FontDescriptor` reachable from one `/Font` entry: its own (a simple font), and each
    of its `/DescendantFonts`' (a Type0 composite font, which is what WeasyPrint emits). `Any` per
    `_pdf_has_embedded_font_file`'s docstring."""
    descriptors: list[Any] = []
    own = font.get("/FontDescriptor")
    if own is not None:
        descriptors.append(own.get_object())
    for descendant_ref in font.get("/DescendantFonts", []):
        descendant = descendant_ref.get_object()
        nested = descendant.get("/FontDescriptor")
        if nested is not None:
            descriptors.append(nested.get_object())
    return descriptors


# --- AC-30(b): the recording fetcher, called zero times on a conformant fixture --------------------


async def test_the_url_fetcher_is_never_called_rendering_a_grammar_conformant_document(
    settings: Settings,
) -> None:
    """Neither the model CV (which carries an `<a href>`) nor the raw-HTML fixture (which never
    becomes markup) gives WeasyPrint a resource to fetch — an anchor is not a resource, and there is
    no `<img>`/`<link>`/`@import`/`@font-face` anywhere the emitter can produce. AC-30(b)."""
    calls: list[str] = []
    renderer = MarkdownDocumentRenderer(settings, url_fetcher=_recording_fetcher(calls))

    await renderer.render(
        MODEL_CV_FIXTURE_MARKDOWN, document=TailoredDocumentKind.CV, format=ExportFormat.PDF
    )

    assert calls == []


def _recording_fetcher(calls: list[str]) -> UrlFetcher:
    """A `UrlFetcher` that records every URL it is asked for, then delegates to the real refusal.

    Annotated `-> NoReturn` and never returning a resource, per `pdf.UrlFetcher`'s own contract — a
    fetcher that could return would not type-check as a substitute for it.
    """

    def fetch(url: str) -> NoReturn:
        calls.append(url)
        refuse_every_url(url)

    return fetch


# --- AC-30(c): hostile HTML fed past the sanitizer, no socket opened -------------------------------


async def test_hostile_html_past_the_sanitizer_renders_with_no_socket_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hand-built HTML — never passed through `sanitize_html` or the emitter — carrying an `<img>`
    and a `<link rel=stylesheet>` pointing at addresses nothing should ever reach. `socket.socket`
    is patched to raise on construction; the render must still succeed, and the fetcher must have
    been called and have refused both resources (AC-30(c), X-54)."""
    calls: list[str] = []

    def _raising_socket(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a socket was opened — the url_fetcher did not intercept the request")

    monkeypatch.setattr(socket, "socket", _raising_socket)

    hostile_html = (
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="http://127.0.0.1:9/evil.css">'
        "</head><body>"
        '<img src="http://127.0.0.1:9/evil.png">'
        "<p>Hello</p>"
        "</body></html>"
    )

    rendered = render_pdf(hostile_html, url_fetcher=_recording_fetcher(calls))

    assert isinstance(rendered, bytes)
    assert len(rendered) > 0
    assert len(calls) >= 2, calls
    assert any("evil.css" in url for url in calls)
    assert any("evil.png" in url for url in calls)


# --- The timeout: an injected 0.01s bound and a monkeypatched slow render --------------------------


async def test_a_render_that_outlasts_its_timeout_is_recorded_render_timed_out(
    settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`asyncio.wait_for` cancels the *await*, never the thread — this proves the adapter turns
    that into `DocumentRenderTimedOut` and logs `reason=render_timed_out` with the exception's type
    and the timeout it was bound to (X-26).

    `monkeypatch.setattr` by string target, restored automatically at teardown — the seam the
    adapter itself offers (`sanitize=`, `url_fetcher=`) covers the PDF branch only, so the TXT
    branch's own module-level `render_plain_text` is swapped directly, the same technique used two
    tests below for the `CancelledError` proof.
    """

    def _sleepy_render_plain_text(tokens: object) -> str:
        time.sleep(0.3)
        return "unreachable"

    monkeypatch.setattr(renderer_module, "render_plain_text", _sleepy_render_plain_text)
    tight_settings = settings.model_copy(update={"export_inline_timeout_seconds": 0.01})
    renderer = MarkdownDocumentRenderer(tight_settings)

    with caplog.at_level(logging.INFO), pytest.raises(DocumentRenderTimedOut):
        await renderer.render("hello", document=TailoredDocumentKind.CV, format=ExportFormat.TXT)

    events = _json_log_events(caplog)
    failed = [e for e in events if e.get("event") == "export.render_failed"]
    assert len(failed) == 1, events
    assert failed[0]["reason"] == "render_timed_out"
    assert failed[0]["error_type"] == "builtins.TimeoutError"
    assert failed[0]["timeout_seconds"] == 0.01


# --- The output cap: an injected 1-byte limit -------------------------------------------------------


async def test_output_over_the_byte_cap_is_refused_and_logged_with_error_type_none(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """X-27, checked on the produced bytes. `error_type=None` on purpose — there is no exception
    here, the render succeeded and the adapter is refusing what it produced, and the four
    `export.render_failed` lines keep one shape (fact 5)."""
    tiny_cap_settings = settings.model_copy(update={"export_max_file_bytes": 1})
    renderer = MarkdownDocumentRenderer(tiny_cap_settings)

    with caplog.at_level(logging.INFO), pytest.raises(DocumentRenderOutputTooLarge):
        await renderer.render(
            "hello world", document=TailoredDocumentKind.CV, format=ExportFormat.MD
        )

    events = _json_log_events(caplog)
    failed = [e for e in events if e.get("event") == "export.render_failed"]
    assert len(failed) == 1, events
    event = failed[0]
    assert event["reason"] == "output_too_large"
    assert "error_type" in event
    assert event["error_type"] is None
    assert event["limit"] == 1
    assert isinstance(event["byte_size"], int)
    assert event["byte_size"] > 1


# --- The floor: an injected raising sanitize --------------------------------------------------------


async def test_an_unrecognised_exception_from_sanitize_becomes_render_error_from_none(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The floor (X-37, AC-34): `sanitize` is the adapter's own testing seam, and a `RuntimeError`
    raised inside it is not one of the measured vendor tuples, so it must fall through to
    `DocumentRenderError`, logged with the type only and re-raised `from None`."""

    def _raising_sanitize(html: str) -> str:
        raise RuntimeError("this message must never reach a log line")

    renderer = MarkdownDocumentRenderer(settings, sanitize=_raising_sanitize)

    with caplog.at_level(logging.INFO), pytest.raises(DocumentRenderError) as exc_info:
        await renderer.render(
            MODEL_CV_FIXTURE_MARKDOWN, document=TailoredDocumentKind.CV, format=ExportFormat.PDF
        )

    assert exc_info.value.__cause__ is None, "raise ... from None must leave __cause__ unset"
    assert exc_info.value.__suppress_context__ is True

    events = _json_log_events(caplog)
    failed = [e for e in events if e.get("event") == "export.render_failed"]
    assert len(failed) == 1, events
    assert failed[0]["reason"] == "render_error"
    assert failed[0]["error_type"] == "builtins.RuntimeError"
    assert "this message must never reach a log line" not in caplog.text


# --- CancelledError is not swallowed (X-36) ----------------------------------------------------------


async def test_cancelled_error_during_a_render_is_not_swallowed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Exception`, never `BaseException`, in the adapter's floor — a worker shutting down mid-render
    must still be able to cancel it."""

    def _slow_render_plain_text(tokens: object) -> str:
        time.sleep(5)
        return "unreachable"

    monkeypatch.setattr(renderer_module, "render_plain_text", _slow_render_plain_text)
    generous_settings = settings.model_copy(update={"export_inline_timeout_seconds": 30})
    renderer = MarkdownDocumentRenderer(generous_settings)

    task = asyncio.ensure_future(
        renderer.render("hello", document=TailoredDocumentKind.CV, format=ExportFormat.TXT)
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- AC-32: no fixture text in any log line across a render -----------------------------------------


async def test_no_document_text_appears_in_any_log_line_across_a_render(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Constitution §8, AC-32: distinctive substrings from the fixture — the candidate's name, a
    role title, a URL — must never appear in any captured log line across all four formats,
    success and (separately, in the floor/timeout/cap tests above) failure."""
    renderer = MarkdownDocumentRenderer(settings)
    sentinels = ["Jordan Rivera", "Senior Backend Engineer", "example.edu", "hexagonal"]

    with caplog.at_level(logging.INFO):
        for format in ExportFormat:
            await renderer.render(
                MODEL_CV_FIXTURE_MARKDOWN, document=TailoredDocumentKind.CV, format=format
            )

    assert caplog.records, "expected the renders to have produced at least one log record"
    for sentinel in sentinels:
        assert sentinel not in caplog.text, f"{sentinel!r} leaked into a log line"


# --- The wiring test: the production binding uses both defaults -------------------------------------


def test_constructing_with_only_settings_binds_both_seams_to_their_defaults(
    settings: Settings,
) -> None:
    """The seams are constructor arguments with strict defaults (fact 3): a caller that supplies
    only `settings` — which is every caller under `api/src/` today — gets the real refusing fetcher
    and the real sanitizer, by identity, never an equivalent stand-in."""
    renderer = MarkdownDocumentRenderer(settings)

    assert renderer._url_fetcher is refuse_every_url
    assert renderer._sanitize is sanitize_html


def test_nothing_under_api_src_constructs_the_renderer_with_a_non_default_seam() -> None:
    """A seam production could take by accident is not a seam. Walks every module under
    `api/src/` for a call to `MarkdownDocumentRenderer(...)` and asserts none passes `url_fetcher=`
    or `sanitize=` — today there is no such call at all (I13's DI wiring lands later), so this is
    also the test that turns that fact from an accident of sequencing into something a future PR
    cannot regress."""
    offending_calls: list[str] = []

    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name != "MarkdownDocumentRenderer":
                continue
            for keyword in node.keywords:
                if keyword.arg in ("url_fetcher", "sanitize"):
                    offending_calls.append(f"{path}:{node.lineno}:{keyword.arg}")

    assert offending_calls == [], offending_calls


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None
