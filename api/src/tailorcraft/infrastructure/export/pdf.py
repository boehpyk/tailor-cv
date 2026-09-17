"""The PDF stage: one stylesheet, one fetcher that refuses everything. ADR-0017 §2 (4) and §5.

WeasyPrint parses an HTML string, resolves CSS against it, and — left to itself — **fetches**
whatever the document points at: images, stylesheets, imported sheets, font sources. Every one of
those goes through a single callable, its `url_fetcher`, which makes this the shortest security
boundary in the slice: one function, no allow-list, no policy, no exceptions.

**This slice opens no socket, and that is a stronger position than a guarded egress.** ADR-0012
governs a request to a *caller-chosen host* — a scheme allow-list in a value object, DNS resolved
before connecting, every resolved address judged, an IP-pinned socket, manual redirects, a streamed
byte cap. None of its ten obligations attach here, because the one library that could open a
connection is handed a fetcher that refuses before a name is ever resolved (ADR-0017 §7). The row in
the failure contract is unreachable **by construction, not by policy**, which is why AC-30's test
asserts *no socket was opened* — a patched `socket.socket` that raises — rather than that a fetch
failed. The day a template wants a remote font, that re-opens ADR-0012 in full, and this paragraph
is the pointer to it.

`base_url=None` is the other half of the same guarantee: with no base, a relative reference in the
document cannot be resolved into a local file path either.

**SKELETON (I1).** `refuse_every_url` and `render_pdf` raise `NotImplementedError`; I3 writes both
bodies. `weasyprint` is deliberately **not imported yet** — the import arrives with the body it
serves, so that this commit adds no vendor dependency to a module that does not yet use one.
`STYLESHEET` and `UrlFetchRefused`, by contrast, are written whole: a constant is its value, the way
`GRAMMAR_RULES` is, so the grep assertion over the stylesheet is green on arrival rather than a
skipped red.
"""

from __future__ import annotations

from typing import Final, NoReturn


class UrlFetchRefused(Exception):
    """Raised for every URL WeasyPrint asks for. There is no input that does not raise it.

    A plain `Exception` subclass, local to this module and deliberately not a domain error: it never
    reaches the domain and never becomes an export's failure reason. WeasyPrint catches a fetcher's
    exception per resource, logs it, and renders the page without that resource — so a document
    pointing at an image produces a PDF without the image, not a failed render. AC-30's test asserts
    the absence of a socket, not the presence of a failure.
    """


# The one stylesheet, the one constant, in the one module (ADR-0017 §5). A4, 18 mm margins, the
# family stack, 10.5 pt, headings scaled, lists indented, links underlined.
#
# It names **no external resource**: no at-rule that pulls in another sheet, and no CSS function
# that takes a location. Asserted by a grep test, because that property is what keeps the fetcher's
# refusal from ever being load-bearing on the honest path — the stylesheet is the *other* thing
# WeasyPrint would go looking for, and a template that could be edited would be an egress with a
# policy rather than no egress at all. Phase 3.2's templates replace this constant in code; until
# then a template is not a setting.
#
# Liberation first, DejaVu second, generic `sans-serif` last, and the order matters operationally:
# WeasyPrint's import succeeds on a box with no fonts installed and the *first render* produces a
# page of boxes, in the worker, where nobody is watching. Two families that real Linux images
# actually ship give that failure two chances not to happen, and AC-46 asserts an embedded font so
# it fails as a red test instead of as a PDF a human has to open.
STYLESHEET: Final[str] = """
@page {
    size: A4;
    margin: 18mm;
}

html {
    font-family: "Liberation Sans", "DejaVu Sans", sans-serif;
    font-size: 10.5pt;
    line-height: 1.45;
    color: #111111;
}

body {
    margin: 0;
}

h1, h2, h3 {
    font-weight: 700;
    margin: 0 0 0.4em;
    page-break-after: avoid;
}

h1 {
    font-size: 17pt;
    margin-top: 0;
}

h2 {
    font-size: 13pt;
    margin-top: 1.1em;
}

h3 {
    font-size: 11.5pt;
    margin-top: 0.9em;
}

p {
    margin: 0 0 0.6em;
    orphans: 2;
    widows: 2;
}

ul, ol {
    margin: 0 0 0.6em;
    padding-left: 6mm;
}

li {
    margin: 0 0 0.15em;
}

li > ul, li > ol {
    margin: 0.15em 0 0;
}

strong {
    font-weight: 700;
}

em {
    font-style: italic;
}

a {
    color: inherit;
    text-decoration: underline;
}
"""


def refuse_every_url(url: str, timeout: float | None = None) -> NoReturn:
    """WeasyPrint's `url_fetcher`, for every scheme and every shape of reference.

    `NoReturn` is the signature saying what the prose says: there is no allowed URL, no permitted
    scheme, and no setting that adds one. AC-30 drives it with `http://`, `https://`,
    `file:///etc/passwd`, `data:` and a bare relative path.

    The log line I3 writes carries the **scheme only** — never the URL. A URL a user typed into
    their own CV can carry their name (X-54, Constitution §8).
    """
    raise NotImplementedError


def render_pdf(html: str) -> bytes:
    """Render sanitized HTML to PDF bytes with `STYLESHEET` and a fetcher that refuses everything.

    CPU-bound and synchronous, like every renderer here; the adapter runs it in
    `asyncio.to_thread` under `asyncio.wait_for` in both processes, never on the event loop.
    """
    raise NotImplementedError
