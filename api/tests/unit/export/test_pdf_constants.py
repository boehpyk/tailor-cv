"""`refuse_every_url` and `STYLESHEET` — the third lock and the one resource WeasyPrint could
otherwise reach for (AC-30(a), ADR-0017 §2 row 4 / §5 / §7).

Pure test: no I/O, no event loop, no fixtures, no mocks, no socket. Every input below is typed out
from AC-30(a)'s list, never read back off the module under test. `refuse_every_url` raises
`NotImplementedError` unconditionally at the time of writing, so every red below is on that
exception escaping the call.

`STYLESHEET` and `UrlFetchRefused` are written whole in the skeleton — a constant is its value — so
the two tests over `STYLESHEET` are **green on arrival**. They assert on the imported constant
itself, never by grepping `pdf.py`'s source text: the module's own `refuse_every_url` function
*name* contains the literal substring `url(`, so a source grep would fail for a reason that has
nothing to do with CSS.
"""

from __future__ import annotations

import pytest

from tailorcraft.infrastructure.export.pdf import STYLESHEET, UrlFetchRefused, refuse_every_url

# --- refuse_every_url: raises on every shape of reference, AC-30(a)'s five inputs -------------------

_REFUSED_URLS: list[tuple[str, str]] = [
    ("http://example.com/logo.png", "http"),
    ("https://example.com/style.css", "https"),
    ("file:///etc/passwd", "file"),
    ("data:image/png;base64,AAAA", "data"),
    ("images/logo.png", "relative_path"),
]


@pytest.mark.parametrize(
    ("url", "_label"),
    _REFUSED_URLS,
    ids=[label for _url, label in _REFUSED_URLS],
)
def test_refuse_every_url_raises_url_fetch_refused(url: str, _label: str) -> None:
    with pytest.raises(UrlFetchRefused):
        refuse_every_url(url)


def test_refuse_every_url_raises_regardless_of_the_timeout_argument() -> None:
    """WeasyPrint's `url_fetcher` protocol may pass a `timeout`; the refusal does not depend on it —
    there is no allowed URL at any timeout."""
    with pytest.raises(UrlFetchRefused):
        refuse_every_url("https://example.com/style.css", timeout=5.0)


# --- STYLESHEET: written whole in the skeleton, so this is green on arrival -------------------------


def test_stylesheet_contains_no_at_import_rule() -> None:
    """An `@import` is a fetch WeasyPrint would attempt, defeating the point of a fetcher that
    refuses everything on a stylesheet that never asks."""
    assert "@import" not in STYLESHEET


def test_stylesheet_contains_no_url_function() -> None:
    """`url(...)` is how CSS names an external resource — a font, an image, another sheet. Asserted
    on the imported constant, never on `pdf.py`'s source text, because that source also contains the
    literal substring `url(` inside the *name* `refuse_every_url`."""
    assert "url(" not in STYLESHEET
