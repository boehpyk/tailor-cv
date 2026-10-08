# ADR-0030: A PDF layout is a closed set of checked-in stylesheets, chosen per export request and recorded on the job

- **Status:** Accepted
- **Date:** 2026-10-08
- **Relates to:** ADR-0016 (an export is a job keyed on run × document × format × run version —
  **amended**: the key gains the layout), ADR-0017 (the rendering pipeline — **amended**: one
  stylesheet constant per layout, and the port gains a keyword), ADR-0015 (a document *is* its
  Markdown — a layout never touches what it says), ADR-0012 (outbound HTTP — **not re-opened**),
  ADR-0004 (everything external crosses a port). Constitution §4's `export` row names the layouts;
  §5's non-goal (*"no layout designer … a small set of templates"*) is **satisfied, not amended**.
  Supersedes nothing.

## Context

Slice 3.2 lets a person choose how their tailored CV and cover letter **look** as a PDF. Since 1.5
there has been exactly one look: one stylesheet in one constant, and ADR-0017 §5 said *"Phase 3.2's
templates replace that one constant; until then a template is not a setting, because the stylesheet
is the other thing WeasyPrint would fetch from."* This ADR is the day that sentence comes due.

Five forces, and they pull in different directions:

1. **A stylesheet is an egress.** WeasyPrint fetches whatever a stylesheet points at — `@import`,
   `url()`, `@font-face` sources. ADR-0017 made that unreachable by construction: one checked-in
   string and a fetcher that refuses every URL. Anything that lets a setting, a row or a user supply
   CSS turns that construction into a policy.
2. **The look is not the content.** ADR-0015 made the Markdown the document. A layout changes no
   word of it, so it does not belong to `tailoring`, and nothing in this slice may move the run's
   `version` (AC-36's empty diff over the LLM boundary and both tailoring layers).
3. **The Constitution's non-goal is explicit**: no layout designer. *"A small set of templates"* is
   the ceiling, not the floor — so no colour picker, no font picker, no "just one option".
4. **Guests and accounts must behave the same.** A guest's rows live 24 hours and are consumed by the
   claim (ADR-0025); a guest has nowhere to keep a preference.
5. **The client must show a picture of each layout**, so it necessarily knows every layout that
   exists — which bears on whether the server should also publish the list.

## Decision

### 1. A layout is a closed `StrEnum` in `domain/export`, with a domain default

```python
class LayoutTemplate(StrEnum):
    CLASSIC = "classic"   # 1.5's stylesheet, byte-for-byte
    MODERN = "modern"
    FORMAL = "formal"

DEFAULT_LAYOUT_TEMPLATE: Final = LayoutTemplate.CLASSIC
```

Three members; definition order is display order. It sits beside `ExportFormat` as a value object of
a rendition. The default is a **domain** decision applied in `RequestExport`, not a Pydantic default
and **never a setting** — a knob on an allow-list is an off switch.

`ExportFormat.takes_layout_template` answers "does this format have a look?" once, on the type, as
`delivery` answers "does this leave the request?": **PDF only**. DOCX is out because a DOCX layout is
a second implementation of every look (paragraph styles in `python-docx`) agreeing with the CSS by
hope, for the one format whose point is that the person restyles it in Word. Markdown and plain text
have no look at all (ADR-0017 §3) and stay byte-identical.

### 2. The choice is per export request, recorded on `ExportJob`

`ExportJob.request` takes a **required** `layout_template` keyword and refuses, before the job
exists, a PDF without one (`LayoutTemplateRequired`) and any other format with one
(`LayoutTemplateNotApplicable`). `ExportRequested` carries it. The column is
`export_job.layout_template`, nullable, and a CHECK pairs it with `format`:
`(format = 'pdf') = (layout_template IS NOT NULL)`. Existing PDF rows are back-filled to `classic`,
which is what they are.

The row that owns a file says which look the file is in. Two layouts of the same version are two
files and, honestly, two rows. Guest and account scopes are identical — the twin routes still share
one handler body.

### 3. The port speaks `LayoutTemplate`; the adapter owns the CSS

`DocumentRendererPort.render(markdown, *, document, format, layout_template)` — **required, no
default**, `None` for a format that takes none. A default of `None` would let a PDF caller forget it
and render nothing in particular.

This is not CSS leaking into the port. **A layout is the user's choice in the domain's words**, as
`format` is; the stylesheet that realizes it is one adapter's implementation and stays in
`infrastructure/export/layouts.py`, chosen by `stylesheet_for(layout)` — an exhaustive `match`
closed by `assert_never`. Swapping WeasyPrint for a print service would still change no domain code:
it would need to know which look was asked for, and nothing about how the old one was drawn.

### 4. Stylesheets are checked-in constants; fonts are the system's

Every layout is one Python string constant. **None of them contains `@import`, `url(`,
`@font-face`, `src:` or `http`**, asserted by one test over every member. Nothing user-supplied is
interpolated into a stylesheet or the HTML shell — `layouts.py` builds no strings at all.

Fonts come only from `fonts-liberation` and `fonts-dejavu-core`, already in the Dockerfile and in CI,
which ship both sans and serif faces; **no package is added**. A missing face is not a runtime
failure — fontconfig substitutes silently — so it is caught as a test that reads each layout's
embedded font family, on CI and inside the production image.

ADR-0017's four obligations are unchanged: `html=False`, the grammar normalization, `nh3` on the
allow-list **before** the document shell is wrapped, and the fetcher that refuses every URL.
ADR-0017 §7's trigger — *"a template with a remote font or a remote image"* — is **not pulled**.

### 5. A layout is never deleted, only retired

Ready rows hold a layout and must keep loading; deleting an enum member would make every list that
contains one a 503. To retire a layout: drop it from the client's list, add a request-path refusal
(`422 layout_template_retired`) in `RequestExport`, and keep its stylesheet until a production read
shows no `queued` or `rendering` row naming it. None of that is built now — nothing is retired — and
the domain test pinning the member set fails with a message pointing here.

### 6. The client mirrors the list, pinned by a test on each side — no catalogue endpoint

The bundle ships one preview image per layout, so it already knows every id. A
`GET /api/layout-templates` would return what the bundle carries, plus a request, a loading state and
an error state that exist only because of the endpoint. 1.5 made the same trade for the four formats.
A Python test and a Vitest test each assert the same three ids in the same order and name the other
file, so drift fails one side.

The client mirrors **ids only**: the default, staleness and retryability stay the server's
(Constitution §4.5). On the wire the field is **optional** — a browser on the pre-3.2 bundle keeps
exporting through the release — and the new client always sends it for a PDF.

### 7. The choice is derived, not remembered

No browser storage (a standing test forbids it) and no account preference. The picker pre-selects
the layout of this run's most recent PDF job, otherwise Classic; a choice made on the page holds for
both document tabs. Across runs it resets to Classic until a PDF exists. That is the honest cost of
storing nothing, and the column on the job is what a later preference would *default* — so a
preference slice undoes nothing here.

### 8. Previews are committed images rendered by the real pipeline, guarded against drift

One WebP per layout, rendered at dev time from the synthetic fixture CV through
`MarkdownDocumentRenderer` (refusing fetcher included), rasterized outside the production image, and
committed. A manifest records each stylesheet's SHA-256 at generation; a test recomputes them and
fails when a stylesheet changed and its picture did not.

## Alternatives

- **The layout on the run.** Remembered per application. Rejected: an export path would mutate
  `tailoring` (ADR-0016's consequence forbids it), a non-content change would need a `version` bump
  or a second counter, and two layouts of one version would become impossible.
- **A user preference now** — the PRD's *"saved template configurations"*. Rejected for this slice:
  guests have nowhere to keep it, it is an `identity` change with its own privacy check and route,
  and nobody has asked for it yet. Deferred, not refused; §7 says what it would build on.
- **A settings-driven or user-supplied stylesheet.** The tempting one, because "let people tweak the
  colour" is a small request. Rejected: a stylesheet is a fetch, so a supplied stylesheet is an
  egress, and a setting that chooses one is an off switch on ADR-0017's construction.
- **Bundled font files via `@font-face` and a fetcher that allows one local directory.** Better
  typography. Rejected: it turns "no egress" into "an egress with a policy" — ADR-0017's rejected
  `base_url` alternative in a new coat.
- **A `rendition` sum type at the port** (`Plain(format) | Pdf(layout)`), making "a layout only for
  PDF" unrepresentable. The purer shape. Rejected for its ripple through `RequestExport`,
  `ExportJob`, the mapping and both use cases, for an invariant the aggregate and the CHECK already
  hold.
- **A catalogue endpoint.** See §6.
- **DOCX layouts.** See §1. Trigger to revisit: asked for at the Phase 3 gate.
- **CSS miniatures drawn in React for the previews.** Rejected: a second copy of every stylesheet,
  in TypeScript. **Previews rendered by the API on demand**: rejected, a render per page view for a
  picture that never changes.
- **A value CHECK on the three ids in the database.** Rejected: `status` and `failure_reason` carry
  none either; the `TypeDecorator` is the authority, and a value CHECK would make a fourth layout a
  migration.

## Consequences

- **ADR-0016's "same request" key widens** to run × document × format × layout × run version, and
  staleness does not: a layout switch makes nothing stale, an edit stales every layout. See its
  amendment.
- **The renderer port changes shape for the first time since it shipped**, and its docstring's list
  of absences loses the word "template". See ADR-0017's amendment.
- **A fourth layout is a checklist, not a design**: a member, a stylesheet constant under the same
  grep, a `match` arm, a preview and its manifest digest, the client's id, both pin tests, and an
  embedded-font assertion.
- **The per-guest and per-run export caps count every layout and do not change.** Every layout of both
  documents plus both DOCX is 8 jobs per version against a per-run cap of 20; the previews exist
  so that nobody has to export three times to compare. Trigger to raise the cap:
  `too_many_export_jobs` observed for a user in production.
- **Retention, the purge, the orphan sweep and the claim are untouched**: every deletion path finds
  an export file by `FileRef.for_export(id, format)`, and a layout changes neither.
- **The downgrade refuses while a non-classic row exists**: pre-3.2 code keys idempotency without
  the layout and would hand a Formal file to a request that can only mean the one look.
- **What to watch:** anyone proposing "just a colour option". That is a layout designer arriving one
  knob at a time, and the answer is a fourth layout or a new ADR, never a parameter.
