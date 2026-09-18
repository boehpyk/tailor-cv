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

`STYLESHEET` is a constant for the same reason `GRAMMAR_RULES` is: it carries no `@import`, no
`url()` and no `@font-face`, so the one document WeasyPrint is handed points at nothing remote
before a stranger's Markdown is even added to it.

**This module is the only one allowed to import `weasyprint`** (ADR-0017's adapter table), which is
why `PDF_DOCUMENT_ERRORS` — the library's own exception types, swept from the installed version —
is exported from here for the adapter to catch by name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, NoReturn
from urllib.parse import urlsplit

import structlog
from weasyprint import CSS, HTML
from weasyprint.css.tokens import InvalidValues, PercentageInMath, RelativeLengthInMath
from weasyprint.images import ImageLoadingError
from weasyprint.svg.utils import PointError
from weasyprint.urls import FatalURLFetchingError, URLFetchingError

log = structlog.get_logger(__name__)

# The adapter's `url_fetcher` seam, typed so that the seam cannot widen into a policy.
# **`NoReturn` is the contract, not documentation**: a fetcher that may return is a fetcher that may
# fetch, and this alias is what makes "there is no allowed URL" a thing `mypy --strict` checks about
# every substitute. AC-30's recording wrapper delegates to `refuse_every_url` and therefore satisfies
# it; a stub that returned a resource would not type-check.
UrlFetcher = Callable[[str], NoReturn]


class UrlFetchRefused(Exception):
    """Raised for every URL WeasyPrint asks for. There is no input that does not raise it.

    A plain `Exception` subclass, local to this module and deliberately not a domain error: it never
    reaches the domain and never becomes an export's failure reason. WeasyPrint catches a fetcher's
    exception per resource, logs it, and renders the page without that resource — so a document
    pointing at an image produces a PDF without the image, not a failed render. AC-30's test asserts
    the absence of a socket, not the presence of a failure.

    That "catches it per resource" is only true because of `_NonFatalFetcher` below; weasyprint 70
    decides between per-resource and fatal by reading an attribute off the fetcher, and a bare
    function does not have it. Read that class before changing anything here.
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
    # `urlsplit` raises on a malformed IPv6 literal, and this function is reached with whatever a
    # stranger's document contains. Refusing is the only outcome; the log line must not be what
    # turns a refusal into an exception of a different type.
    try:
        scheme = urlsplit(url).scheme or "none"
    except ValueError:
        scheme = "unparseable"
    log.info("export.url_fetch_refused", scheme=scheme)
    # A constant message. Interpolating the URL here would put it in the exception, and WeasyPrint
    # logs a fetcher's exception itself — which is how a stranger's URL would reach our logs by a
    # route nobody wrote.
    raise UrlFetchRefused("This renderer fetches nothing.")


class _NonFatalFetcher:
    """Wraps a `UrlFetcher` so WeasyPrint 70 can handle its refusal the way it documents.

    **Measured against weasyprint 70.0, and it is not what the plain-function API used to do.**
    `weasyprint.urls.fetch` calls the fetcher, catches `Exception`, and then reads
    `url_fetcher._fail_on_errors` to decide between a per-resource `URLFetchingError` (caught by the
    caller, which logs and renders without the resource) and a `FatalURLFetchingError` that stops
    the render. A plain function has no such attribute, so the refusal came back out as
    `AttributeError: 'function' object has no attribute '_fail_on_errors'` — raised *inside* the
    library's own error handler, aborting the whole render. Every document that so much as mentions
    an external resource would have failed instead of rendering without it, and the adapter's floor
    would have recorded it as `render_error` with no way to tell why.

    `False` is the value this pipeline wants, and deliberately so: WeasyPrint then renders the page
    without the resource, which is exactly what AC-30(c) and X-54 promise. `True` would raise
    `FatalURLFetchingError`, which subclasses **`BaseException`** — an `except Exception` floor
    cannot catch it, and the port's promise would break on the one path it exists for.

    A wrapper rather than an attribute bolted onto `refuse_every_url`, because the seam accepts any
    `UrlFetcher` and a test's recording wrapper has no more `_fail_on_errors` than a bare function
    does. Every fetcher reaching WeasyPrint goes through this class, so the library's contract is
    satisfied for all of them.
    """

    # Read by `weasyprint.urls.fetch`. Private in the library and named here on purpose: this is a
    # measured fact about weasyprint 70.0 and it is what a version bump has to re-check.
    _fail_on_errors = False

    def __init__(self, fetch: UrlFetcher) -> None:
        self._fetch = fetch

    # **The signature matches `refuse_every_url`'s, and that is not cosmetic.** Measured on
    # weasyprint 70.0: `weasyprint.urls.fetch` calls `url_fetcher(url)` **positionally, with no
    # timeout**, so a narrower `(self, url)` is unreachable today — and unreachable is the whole
    # problem. A version bump that starts passing `timeout=` would make this wrapper raise
    # `TypeError` *inside* the library's own `except`, where `_fail_on_errors = False` turns it into
    # an ordinary non-fatal fetch failure: the render would quietly succeed, the
    # `export.url_fetch_refused` log line would never be written and `UrlFetchRefused` would never
    # be raised. The refusal would still hold (nothing is fetched), but the evidence that it held
    # would be gone. Accepting the argument costs nothing and removes the trap.
    #
    # Accepted and **not forwarded**, deliberately: `UrlFetcher` is `Callable[[str], NoReturn]`, so
    # a substitute — AC-30's recording wrapper — takes one argument, and `refuse_every_url` ignores
    # its own `timeout` anyway because it raises before it could use one. A fetcher that fetches
    # nothing has no deadline to honour.
    def __call__(self, url: str, timeout: float | None = None) -> NoReturn:
        self._fetch(url)


# WeasyPrint's own exception types that can escape `write_pdf`, measured against **weasyprint 70.0**
# by sweeping every module in the package for `BaseException` subclasses. They are the *specific*
# translations that sit on top of `MarkdownDocumentRenderer`'s floor, carrying the better reason
# (`render_failed`, the document's own fault) instead of the residual `render_error`.
#
# They live here rather than in the adapter because `pdf.py` is the only module allowed to import
# `weasyprint` (ADR-0017's adapter table), so the adapter catches this tuple by name.
#
# **`FatalURLFetchingError` subclasses `BaseException`, not `Exception`** — the one measured hole in
# the floor, and the reason this tuple is typed `type[BaseException]`. `_NonFatalFetcher` makes it
# unreachable today; naming it anyway is what keeps the port's promise true if that ever changes,
# and it is safe to catch by name in a way `except BaseException` never is (X-36).
PDF_DOCUMENT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    FatalURLFetchingError,
    URLFetchingError,
    InvalidValues,
    PercentageInMath,
    RelativeLengthInMath,
    ImageLoadingError,
    PointError,
)


def render_pdf(html: str, *, url_fetcher: UrlFetcher = refuse_every_url) -> bytes:
    """Render sanitized HTML to PDF bytes with `STYLESHEET` and a fetcher that refuses everything.

    CPU-bound and synchronous, like every renderer here; the adapter runs it in
    `asyncio.to_thread` under `asyncio.wait_for` in both processes, never on the event loop.

    `url_fetcher` is the adapter's testing seam arriving from `MarkdownDocumentRenderer`, with the
    refusing fetcher as its strict default. Nothing under `api/src/` ever passes it; AC-30(b) wraps
    the default in a recorder and asserts the count is zero on a conformant document.
    """
    document = HTML(string=html, base_url=None, url_fetcher=_NonFatalFetcher(url_fetcher))
    rendered = document.write_pdf(stylesheets=[CSS(string=STYLESHEET)])
    # WeasyPrint 70 ships no type information, so `write_pdf` is `Any` here; its signature is
    # `write_pdf(target=None, ...)` and it returns the bytes only when `target` is None — which is
    # why the narrowing is a real check rather than a `cast`. If a future version ever changes that,
    # this raises where the adapter's `except Exception` floor can translate it, instead of handing
    # `None` to a caller annotated `bytes`.
    if not isinstance(rendered, bytes):
        raise TypeError(f"weasyprint.write_pdf returned {type(rendered).__name__}, not bytes")
    return rendered
