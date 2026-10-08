"""The PDF layouts: one checked-in stylesheet per `LayoutTemplate`. ADR-0030, ADR-0017 §5 (amended).

`LayoutTemplate` is the user's choice in the domain's words; the CSS that realizes it is this
adapter's, and it lives here as **plain string constants**. Not a setting, not a file read at
runtime, not a template filled in from anything: a stylesheet that could be assembled from input
would be a way for that input to name a resource, and WeasyPrint fetches what CSS names. So every
constant below is one literal — no f-string, no `%`, no `.format`, no concatenation (AC-16 pins that
with an AST test) — and none carries an `@import`, a `url(`, an `@font-face` or a `src:` (AC-15).
The fetcher in `pdf.py` refuses every URL anyway; these sheets are why it never has to.

**Fonts are the families the image already ships**, `fonts-liberation` and `fonts-dejavu-core`,
both of which carry Sans *and* Serif faces, so no layout adds a package. Liberation first, DejaVu
second, one generic keyword last: fontconfig substitutes silently for a family it cannot find, so a
missing face is a PDF in the wrong typeface rather than an error (L-22). AC-18 reads each layout's
embedded `BaseFont` back out of a real render to turn that into a red test.

All three are single column in reading order (ATS parsers read columns badly), near-black text, no
backgrounds and no images. They style the same eleven tags `html.py` emits; that module is shared
and unchanged by a layout, so the layout id never reaches the HTML (AC-16).

**This module imports nothing from WeasyPrint** — the constants are strings — so the drift tests
can import it without the system libraries.
"""

from __future__ import annotations

from typing import Final, assert_never

from tailorcraft.domain.export.value_objects import LayoutTemplate

# 1.5's stylesheet, **moved byte-for-byte** from `pdf.STYLESHEET` (AC-14 pins its SHA-256 from
# `main`). Do not reformat it: Classic is the promise that a PDF requested with no layout looks
# exactly as it did before layouts existed. A4, 18 mm, 10.5 pt, headings scaled, links underlined.
CLASSIC_STYLESHEET: Final[str] = """
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

# Modern: the same sans, tighter page, one accent colour for structure only. The name sits on a 2 pt
# rule; section headings are small uppercase labels on a hairline. Bullets are en dashes, written as
# the CSS escape `\2013` (doubled backslash below, so Python leaves it for CSS) rather than the
# character, which keeps this file ASCII. The escape eats one following space; the second is the gap.
#
# **`list-style-type: "<string>"`, not `li::marker { content: … }`** — measured on weasyprint 70.0:
# `::marker` with `content` raises `TypeError: min-content width for TextBox not handled yet` from
# its layout code on any `<ul>`, so every Modern CV would have failed `render_error`. The string
# value is CSS Lists 3's other spelling of the same marker and renders.
MODERN_STYLESHEET: Final[str] = """
@page {
    size: A4;
    margin: 16mm;
}

html {
    font-family: "Liberation Sans", "DejaVu Sans", sans-serif;
    font-size: 10pt;
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
    font-size: 20pt;
    margin-top: 0;
    margin-bottom: 0.6em;
    padding-bottom: 1.5mm;
    border-bottom: 2pt solid #1d4e63;
}

h2 {
    font-size: 9.5pt;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: #1d4e63;
    margin-top: 1.4em;
    padding-bottom: 0.8mm;
    border-bottom: 0.5pt solid #1d4e63;
}

h3 {
    font-size: 10.5pt;
    margin-top: 0.9em;
}

p {
    margin: 0 0 0.55em;
    orphans: 2;
    widows: 2;
}

ul, ol {
    margin: 0 0 0.55em;
    padding-left: 5mm;
}

ul {
    list-style-type: "\\2013  ";
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

# Formal: a serif, wider margins, generous leading. The name centred; section headings in small
# capitals on a hairline. No colour beyond near-black.
FORMAL_STYLESHEET: Final[str] = """
@page {
    size: A4;
    margin: 20mm;
}

html {
    font-family: "Liberation Serif", "DejaVu Serif", serif;
    font-size: 10.5pt;
    line-height: 1.6;
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
    font-size: 18pt;
    text-align: center;
    margin-top: 0;
    margin-bottom: 0.8em;
}

h2 {
    font-size: 12pt;
    font-variant: small-caps;
    letter-spacing: 0.04em;
    margin-top: 1.3em;
    padding-bottom: 0.8mm;
    border-bottom: 0.5pt solid #111111;
}

h3 {
    font-size: 11pt;
    margin-top: 1em;
}

p {
    margin: 0 0 0.7em;
    orphans: 2;
    widows: 2;
}

ul, ol {
    margin: 0 0 0.7em;
    padding-left: 6mm;
}

li {
    margin: 0 0 0.2em;
}

li > ul, li > ol {
    margin: 0.2em 0 0;
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


def stylesheet_for(layout: LayoutTemplate) -> str:
    """The module constant for `layout` — returned as is, never copied or built (AC-16's identity).

    Closed by `assert_never`, so a fourth `LayoutTemplate` stops `mypy --strict` here by name.
    """
    match layout:
        case LayoutTemplate.CLASSIC:
            return CLASSIC_STYLESHEET
        case LayoutTemplate.MODERN:
            return MODERN_STYLESHEET
        case LayoutTemplate.FORMAL:
            return FORMAL_STYLESHEET
        case _:
            assert_never(layout)
