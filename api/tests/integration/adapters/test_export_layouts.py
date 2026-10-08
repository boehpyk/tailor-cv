"""AC-15…AC-20: the three PDF layouts through the real renderer (3.2, T15, test-after).

AC-10 (`md`/`txt` digests) and AC-14 (Classic's digest, the shell digests) are already pinned from
`main` by `test_export_golden_digests.py` (T9) and are not repeated here.

- **AC-15** reads every `LayoutTemplate` member through `stylesheet_for`, never a typed-out list.
- **AC-16** has two halves: the stylesheet handed to WeasyPrint `is` the module constant (a recording
  `render_pdf` seam), and an AST scan of `layouts.py` finds no f-string, `%`, `+`, `.format` or call
  that could build CSS from anything.
- **AC-18** is the one test that can see a *silent fontconfig substitution*: it reads each embedded
  font's `BaseFont` back out of a real render. `@pytest.mark.slow` so T27 can run exactly it inside
  the production image. Real WeasyPrint names are hyphenated (`Liberation-Sans`) and Modern/Formal
  alter case by design (`EXPERIENCE`, `ExpEriEncE`), hence the case-insensitive match (AC-18 as
  amended at T13).
"""

from __future__ import annotations

import ast
import io
import logging
import re
import socket
from pathlib import Path
from typing import Any, Final, NoReturn

import pytest
from pypdf import PdfReader

from tailorcraft.domain.export.value_objects import ExportFormat, LayoutTemplate
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export import layouts as layouts_module
from tailorcraft.infrastructure.export import renderer as renderer_module
from tailorcraft.infrastructure.export.html import sanitize_html
from tailorcraft.infrastructure.export.layouts import stylesheet_for
from tailorcraft.infrastructure.export.pdf import UrlFetcher, refuse_every_url, render_pdf
from tailorcraft.infrastructure.export.renderer import MarkdownDocumentRenderer
from tailorcraft.infrastructure.settings import Settings
from tests.fixtures.documents import (
    MARKDOWN_FIXTURE_CORPUS,
    MODEL_CV_FIXTURE_MARKDOWN,
    MODEL_LETTER_FIXTURE_MARKDOWN,
    MarkdownFixture,
)

_ALL = list(LayoutTemplate)
_IDS = [m.value for m in LayoutTemplate]
_LAYOUTS_SOURCE: Final = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "tailorcraft"
    / "infrastructure"
    / "export"
    / "layouts.py"
)
_CONSTANT_OF: Final = {
    LayoutTemplate.CLASSIC: "CLASSIC_STYLESHEET",
    LayoutTemplate.MODERN: "MODERN_STYLESHEET",
    LayoutTemplate.FORMAL: "FORMAL_STYLESHEET",
}
_SHIPPED_FAMILIES: Final = {"Liberation Sans", "DejaVu Sans", "Liberation Serif", "DejaVu Serif"}
_GENERIC_FAMILIES: Final = {"sans-serif", "serif"}


# --- AC-15: nothing in a stylesheet can make WeasyPrint reach anything --------------------------


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
@pytest.mark.parametrize("token", ["@import", "url(", "@font-face", "src:", "image(", "http", "//"])
def test_no_stylesheet_contains_a_token_that_names_a_resource(
    layout: LayoutTemplate, token: str
) -> None:
    assert token not in stylesheet_for(layout)


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
def test_every_font_family_stack_names_only_shipped_families_then_one_generic(
    layout: LayoutTemplate,
) -> None:
    stacks = re.findall(r"font-family:\s*([^;]+);", stylesheet_for(layout))
    assert stacks, "no font-family declaration found, so this test would pass vacuously"
    for stack in stacks:
        names = [n.strip().strip("\"'") for n in stack.split(",")]
        *families, generic = names
        assert families, stack
        assert set(families) <= _SHIPPED_FAMILIES, stack
        assert generic in _GENERIC_FAMILIES, stack


def test_the_three_layouts_are_three_different_stylesheets() -> None:
    assert len({stylesheet_for(layout) for layout in _ALL}) == len(_ALL)


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
def test_stylesheet_for_returns_the_module_constant_itself(layout: LayoutTemplate) -> None:
    assert stylesheet_for(layout) is getattr(layouts_module, _CONSTANT_OF[layout])


def test_the_constant_table_covers_every_layout() -> None:
    assert set(_CONSTANT_OF) == set(LayoutTemplate)


# --- AC-16: nothing user-supplied reaches CSS or the HTML shell ---------------------------------


def test_layouts_py_builds_no_string_from_anything() -> None:
    """No f-string, `%`, `+`, `.format` or `join`; every `*_STYLESHEET` is one string literal.
    **Mutation observed red**: adding an f-string to `FORMAL_STYLESHEET` fails the first assertion."""
    tree = ast.parse(_LAYOUTS_SOURCE.read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            offenders.append(f"f-string at line {node.lineno}")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod | ast.Add):
            offenders.append(f"{type(node.op).__name__} at line {node.lineno}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"format", "format_map", "join", "replace"}
        ):
            offenders.append(f".{node.func.attr}() at line {node.lineno}")
    assert offenders == []

    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assert node.value is not None
            constants[node.target.id] = node.value
    assert set(constants) >= set(_CONSTANT_OF.values()), "constants not found: vacuous"
    for name in _CONSTANT_OF.values():
        value = constants[name]
        assert isinstance(value, ast.Constant), f"{name} is not a plain literal"
        assert isinstance(value.value, str)


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
@pytest.mark.parametrize("document", list(TailoredDocumentKind))
async def test_the_stylesheet_handed_to_weasyprint_is_the_module_constant_and_the_html_has_no_layout(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    layout: LayoutTemplate,
    document: TailoredDocumentKind,
) -> None:
    seen: list[tuple[str, str]] = []

    def recording(
        html: str, *, stylesheet: str, url_fetcher: UrlFetcher = refuse_every_url
    ) -> bytes:
        seen.append((html, stylesheet))
        return render_pdf(html, stylesheet=stylesheet, url_fetcher=url_fetcher)

    monkeypatch.setattr(renderer_module, "render_pdf", recording)
    markdown = (
        MODEL_CV_FIXTURE_MARKDOWN
        if document is TailoredDocumentKind.CV
        else MODEL_LETTER_FIXTURE_MARKDOWN
    )

    await MarkdownDocumentRenderer(settings).render(
        markdown, document=document, format=ExportFormat.PDF, layout_template=layout
    )

    assert len(seen) == 1
    html, stylesheet = seen[0]
    assert stylesheet is getattr(layouts_module, _CONSTANT_OF[layout])
    for other in LayoutTemplate:
        assert f'"{other.value}"' not in html
        assert f"layout-{other.value}" not in html
    assert "font-family" not in html


# --- AC-17: 1.5's security tests, for every layout ----------------------------------------------


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
async def test_the_url_fetcher_is_never_called_for_a_conformant_document_under_any_layout(
    settings: Settings, layout: LayoutTemplate
) -> None:
    calls: list[str] = []

    def fetch(url: str) -> NoReturn:
        calls.append(url)
        refuse_every_url(url)

    renderer = MarkdownDocumentRenderer(settings, url_fetcher=fetch)
    for fixture in MARKDOWN_FIXTURE_CORPUS:
        await renderer.render(
            fixture.markdown,
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            layout_template=layout,
        )

    assert calls == []


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
def test_hostile_html_past_the_parser_renders_with_no_socket_opened_under_any_layout(
    monkeypatch: pytest.MonkeyPatch, layout: LayoutTemplate
) -> None:
    calls: list[str] = []

    def raising_socket(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a socket was opened")

    def fetch(url: str) -> NoReturn:
        calls.append(url)
        refuse_every_url(url)

    monkeypatch.setattr(socket, "socket", raising_socket)
    hostile = (
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="http://127.0.0.1:9/evil.css">'
        "</head><body>"
        '<img src="http://127.0.0.1:9/evil.png"><p>Hello</p></body></html>'
    )

    rendered = render_pdf(hostile, stylesheet=stylesheet_for(layout), url_fetcher=fetch)

    assert rendered.startswith(b"%PDF")
    assert any("evil.css" in u for u in calls), calls
    assert any("evil.png" in u for u in calls), calls


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
async def test_nh3_still_strips_what_the_parser_could_not_have_produced_under_any_layout(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, layout: LayoutTemplate
) -> None:
    """Hostile markup arrives at the sanitize seam; whatever `sanitize_html` lets through is what
    WeasyPrint is handed. No `<img>`, `<script>`, `<link>` or handler survives, so nothing is
    fetched and no socket opens."""
    handed: list[str] = []

    def hostile_then_real_sanitize(fragment: str) -> str:
        cleaned = sanitize_html(
            '<p onclick="x()">kept</p><script>alert(1)</script>'
            '<img src="http://127.0.0.1:9/evil.png" onerror="x()">'
            '<link rel="stylesheet" href="http://127.0.0.1:9/evil.css">'
            '<style>@import "http://127.0.0.1:9/e.css"</style>'
        )
        handed.append(cleaned)
        return cleaned

    def raising_socket(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a socket was opened")

    calls: list[str] = []

    def fetch(url: str) -> NoReturn:
        calls.append(url)
        refuse_every_url(url)

    monkeypatch.setattr(socket, "socket", raising_socket)
    renderer = MarkdownDocumentRenderer(
        settings, sanitize=hostile_then_real_sanitize, url_fetcher=fetch
    )

    rendered = await renderer.render(
        "# x\n", document=TailoredDocumentKind.CV, format=ExportFormat.PDF, layout_template=layout
    )

    assert rendered.startswith(b"%PDF")
    assert len(handed) == 1
    for forbidden in ("<script", "<img", "<link", "<style", "onclick", "onerror", "evil"):
        assert forbidden not in handed[0], forbidden
    assert "kept" in handed[0]
    assert calls == []


# --- AC-18: a real render, read back ------------------------------------------------------------

_SANS: Final = re.compile(r"^(Liberation-?Sans|DejaVu-?Sans)", re.IGNORECASE)
_SERIF: Final = re.compile(r"^(Liberation-?Serif|DejaVu-?Serif)", re.IGNORECASE)
_FAMILY_OF: Final = {
    LayoutTemplate.CLASSIC: _SANS,
    LayoutTemplate.MODERN: _SANS,
    LayoutTemplate.FORMAL: _SERIF,
}


def _embedded_fonts(reader: PdfReader) -> list[tuple[str, bool]]:
    """`(BaseFont without its subset prefix, has /FontFile*)` for every font on every page.
    `Any`: a PDF object graph is dict-of-dicts by the format's design (see test_document_renderer)."""
    found: list[tuple[str, bool]] = []
    for page in reader.pages:
        resources: Any = page.get("/Resources")
        fonts = resources.get("/Font") if resources is not None else None
        if fonts is None:
            continue
        for ref in fonts.values():
            font: Any = ref.get_object()
            name = re.sub(r"^[A-Z]{6}\+", "", str(font.get("/BaseFont", "")).lstrip("/"))
            embedded = False
            descriptors: list[Any] = []
            if font.get("/FontDescriptor") is not None:
                descriptors.append(font["/FontDescriptor"].get_object())
            for d in font.get("/DescendantFonts", []):
                nested = d.get_object().get("/FontDescriptor")
                if nested is not None:
                    descriptors.append(nested.get_object())
            for descriptor in descriptors:
                embedded = embedded or any(
                    k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3")
                )
            found.append((name, embedded))
    return found


@pytest.mark.slow
@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
@pytest.mark.parametrize("document", list(TailoredDocumentKind))
async def test_a_real_render_reads_back_in_order_with_an_embedded_font_of_the_layouts_family(
    settings: Settings, layout: LayoutTemplate, document: TailoredDocumentKind
) -> None:
    """**Mutation observed red**: pointing `FORMAL_STYLESHEET`'s `html` font-family at
    `"Liberation Sans", "DejaVu Sans", sans-serif` fails the family assertion for Formal (and only
    Formal), with the rendered `BaseFont` names in the message."""
    if document is TailoredDocumentKind.CV:
        markdown, first, second = MODEL_CV_FIXTURE_MARKDOWN, "jordan rivera", "experience"
    else:
        markdown, first, second = MODEL_LETTER_FIXTURE_MARKDOWN, "dear hiring manager", "sincerely"

    rendered = await MarkdownDocumentRenderer(settings).render(
        markdown, document=document, format=ExportFormat.PDF, layout_template=layout
    )

    reader = PdfReader(io.BytesIO(rendered))
    text = reader.pages[0].extract_text().lower()
    assert first in text, text
    assert second in text, text
    assert text.index(first) < text.index(second), "reading order broke (a second column?)"

    fonts = _embedded_fonts(reader)
    assert any(embedded for _name, embedded in fonts), fonts
    wanted = _FAMILY_OF[layout]
    wrong = [name for name, _e in fonts if not wanted.match(name)]
    assert fonts
    assert not wrong, f"{layout.value}: wrong family in {wrong}; all fonts: {fonts}"


# --- AC-19: every corpus document, both kinds, every layout ------------------------------------


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
@pytest.mark.parametrize("kind", list(TailoredDocumentKind))
@pytest.mark.parametrize(
    "fixture", MARKDOWN_FIXTURE_CORPUS, ids=[f.name for f in MARKDOWN_FIXTURE_CORPUS]
)
async def test_every_corpus_document_renders_to_a_pdf_under_every_layout(
    settings: Settings,
    fixture: MarkdownFixture,
    kind: TailoredDocumentKind,
    layout: LayoutTemplate,
) -> None:
    rendered = await MarkdownDocumentRenderer(settings).render(
        fixture.markdown, document=kind, format=ExportFormat.PDF, layout_template=layout
    )

    assert rendered.startswith(b"%PDF")
    assert 0 < len(rendered) <= 20 * 1024 * 1024
    assert len(PdfReader(io.BytesIO(rendered)).pages) >= 1


# --- AC-20: the log fields ----------------------------------------------------------------------


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    import json

    out: list[dict[str, Any]] = []
    for record in caplog.records:
        try:
            event = json.loads(record.getMessage())
        except json.JSONDecodeError:
            continue
        if event.get("event") == name:
            out.append(event)
    return out


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
async def test_started_and_succeeded_carry_the_layout_id_for_a_pdf(
    settings: Settings, caplog: pytest.LogCaptureFixture, layout: LayoutTemplate
) -> None:
    with caplog.at_level(logging.INFO):
        await MarkdownDocumentRenderer(settings).render(
            MODEL_CV_FIXTURE_MARKDOWN,
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            layout_template=layout,
        )

    started = _events(caplog, "export.render_started")
    succeeded = _events(caplog, "export.render_succeeded")
    assert len(started) == 1
    assert len(succeeded) == 1
    assert started[0]["layout_template"] == layout.value
    assert succeeded[0]["layout_template"] == layout.value


@pytest.mark.parametrize("fmt", [ExportFormat.DOCX, ExportFormat.MD, ExportFormat.TXT])
async def test_started_and_succeeded_carry_null_for_a_format_with_no_layout(
    settings: Settings, caplog: pytest.LogCaptureFixture, fmt: ExportFormat
) -> None:
    with caplog.at_level(logging.INFO):
        await MarkdownDocumentRenderer(settings).render(
            MODEL_CV_FIXTURE_MARKDOWN,
            document=TailoredDocumentKind.CV,
            format=fmt,
            layout_template=None,
        )

    for name in ("export.render_started", "export.render_succeeded"):
        (event,) = _events(caplog, name)
        assert "layout_template" in event
        assert event["layout_template"] is None


async def test_failed_carries_the_layout_with_reason_and_error_type_and_no_message(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    def raising(html: str) -> str:
        raise RuntimeError("secret message with <html> and font-family")

    with caplog.at_level(logging.INFO), pytest.raises(Exception, match=r"."):
        await MarkdownDocumentRenderer(settings, sanitize=raising).render(
            MODEL_CV_FIXTURE_MARKDOWN,
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            layout_template=LayoutTemplate.FORMAL,
        )

    (failed,) = _events(caplog, "export.render_failed")
    assert failed["layout_template"] == "formal"
    assert failed["reason"] == "render_error"
    assert failed["error_type"] == "builtins.RuntimeError"
    assert "secret message" not in caplog.text


async def test_a_pdf_with_no_layout_is_a_render_error_not_a_default_and_logs_null(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """L-30: unreachable through the aggregate and the CHECK; if reached, a recorded failure,
    never a silent Classic."""
    from tailorcraft.domain.export.errors import DocumentRenderError

    with caplog.at_level(logging.INFO), pytest.raises(DocumentRenderError):
        await MarkdownDocumentRenderer(settings).render(
            MODEL_CV_FIXTURE_MARKDOWN,
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            layout_template=None,
        )

    (failed,) = _events(caplog, "export.render_failed")
    assert failed["layout_template"] is None
    assert failed["reason"] == "render_error"
    assert failed["error_type"].endswith("LayoutTemplateMissing")
    assert not _events(caplog, "export.render_succeeded")


@pytest.mark.parametrize("layout", _ALL, ids=_IDS)
async def test_no_log_line_carries_css_html_or_document_text_under_any_layout(
    settings: Settings, caplog: pytest.LogCaptureFixture, layout: LayoutTemplate
) -> None:
    with caplog.at_level(logging.INFO):
        await MarkdownDocumentRenderer(settings).render(
            MODEL_CV_FIXTURE_MARKDOWN,
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            layout_template=layout,
        )

    assert caplog.records
    for needle in ("font-family", "@page", "<html", "<p>", "Jordan Rivera", "hexagonal"):
        assert needle not in caplog.text, needle
