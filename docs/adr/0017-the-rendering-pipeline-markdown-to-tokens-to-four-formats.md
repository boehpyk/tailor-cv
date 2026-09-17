# ADR-0017: The rendering pipeline — Markdown → normalized tokens → sanitized HTML → PDF; the same tokens → DOCX and plain text

- **Status:** Accepted
- **Date:** 2026-09-17
- **Relates to:** ADR-0015 §2 (the three obligations 1.5 inherits — **executed here**) and its open
  question about TXT (**closed here**), ADR-0015 §5 (the editor sanitizes by construction, and the
  limit of that argument — this ADR is the other side of that limit), ADR-0004 (the LLM's output is
  untrusted text), ADR-0005 (PDF and DOCX leave the request because of the cost of the work),
  ADR-0009 (a CPU-bound call goes in a thread), ADR-0012 (outbound HTTP is a guarded egress — and
  **why this slice takes on none of its obligations**), ADR-0016 (the export job). Supersedes
  nothing.

## Context

Slice 1.5 turns a document into a file, and it is the first time in this codebase that a stranger's
text becomes **HTML**. 1.4 could argue itself out of a sanitizer: the editor goes Markdown → tokens
→ ProseMirror nodes → `toDOM()`, so no HTML string ever exists on the client and there is nothing
for a sanitizer to sanitize (ADR-0015 §5). That argument does not survive a PDF. WeasyPrint takes an
HTML string, parses it, resolves CSS, and — left to itself — **fetches** whatever the document
points at.

So three things have to be decided at once, and they are entangled:

1. **What is the pipeline, and which stage owns which hostile input?** ADR-0015 §2 named three
   obligations — `html=False`, `nh3` on the grammar's allow-list, a `url_fetcher` that refuses
   everything — but not their shape, their order, or what each one is actually responsible for. A
   list of three mitigations with no owner is how two of them end up guarding the same thing and the
   third guards nothing.
2. **What is "plain text"?** ADR-0015 left it open in as many words: the Markdown itself, or a
   marker-stripped rendering?
3. **Does DOCX go through HTML?** It is the shortest path in a hurry, and it would put a third
   consumer behind the sanitizer.

The forces: four formats and only one source of truth (the stored Markdown); the closed document
grammar the editor already enforces, which the PDF must match or the user will think the export
broke their document; a worker process where a vendor exception message can carry a quoted CV into
Sentry; and a hard rule that a knob on an allow-list is an off switch.

## Decision

### 1. One parse, one normalization, three walkers

```
stored Markdown (TailoredCv | CoverLetter)
   │  markdown-it-py, preset "zero" + the grammar's rules, html=False,
   │  validateLink = the three schemes
   ▼
token stream ──► normalize_to_grammar() ──► h4+ clamped to h3, every foreign
   │                                        block/inline type replaced by text,
   │                                        images unrepresentable
   ├──► render_plain_text(tokens) ─────────────────────────────────► bytes (txt)
   ├──► render_docx(tokens, document) via python-docx ─────────────► bytes (docx)
   └──► render_html(tokens, document) ──► nh3.clean(allow-list)
                                            ──► WeasyPrint(url_fetcher=refuse) ─► bytes (pdf)

md: the stored Markdown, encoded. No parse — the stored text is the export.
```

`parse_document`, `normalize_to_grammar`, `render_plain_text`, `render_html`, `sanitize_html` and
`render_pdf` are **pure functions in their own modules**, each with its own test. The adapter
`MarkdownDocumentRenderer` is a `match` over the format and nothing else.

### 2. The four obligations, with an owner each

ADR-0015 §2's three, executed, plus the one it did not name:

| # | Obligation | Owner | What it is responsible for | What it is **not** |
|---|---|---|---|---|
| 1 | **`html=False`, closed rule set, `validateLink`** | `tokens.py::parse_document` | Raw HTML never becomes markup — it becomes a text token. `image` is not an enabled rule, so an image is unrepresentable. A link's scheme is `http`, `https` or `mailto` or the URL is dropped. | a sanitizer of HTML it did not produce |
| 2 | **The grammar normalization** | `tokens.py::normalize_to_grammar` | Everything the three walkers see is inside the closed grammar: `h4`+ clamped to `h3`, tables / fences / blockquotes / rules / code / strikethrough as text, list nesting bounded. | a security boundary on its own — it is the *rendering* half, and it is what makes the PDF match the editor |
| 3 | **`nh3` on the grammar's allow-list** | `html.py::sanitize_html` | The **second lock**: eleven tags, `href`/`title` on `a`, `start` on `ol`, three URL schemes, `rel="noopener noreferrer"`, comments stripped. | the first lock. It exists for the day someone enables a rule or upgrades the parser |
| 4 | **A `url_fetcher` that refuses everything** | `pdf.py::refuse_every_url` | WeasyPrint's *one* way of reaching the network or the filesystem — images, stylesheets, `@import`s, `@font-face` sources — all arrive through one callable, and it raises on every input. `base_url=None` means a relative URL cannot resolve into a `file://` path either. | a policy with exceptions. There is no allowed URL and no setting that adds one |

**`nh3` is the second lock, not the first, and that is load-bearing.** With `html=False` and a
closed rule set the emitter can only produce the eleven tags on the allow-list, so on the honest
path `nh3` removes nothing. Its test therefore feeds it hand-built hostile HTML **past the parser**,
through the adapter's sanitize seam, and proves it holds on its own — otherwise the test would pass
for the wrong reason forever, which is the same defect as a gate that checks nothing.

### 3. Plain text is a marker-stripped rendering, and bullets and numbers stay

ADR-0015's open question, closed. A "plain text CV" is what a person pastes into an application
form's textarea. `**` and `##` in that box read as broken, and the base CV's own extracted text
(1.1) never had them.

- **Goes:** `#`, `**`, `*`, `[...](...)` syntax, and every HTML-looking thing (there is none in the
  stream to begin with — `html=False` already made it text, so plain text has nothing to strip).
- **Stays:** `- ` for a bullet and `1.` for an ordered item. **They are plain-text structure, not
  markup** — a list pasted without them is a paragraph. Heading text sits on its own line; a link
  renders as `text (https://…)`, or as the text alone when the scheme was refused; a hard break is a
  newline; blocks are separated by one blank line.

**Markdown is the format for whoever wants the source**, which is the other half of why `txt` does
not have to be it.

### 4. DOCX walks the tokens, never the HTML

`python-docx` has no HTML importer and should not grow one here. The walker maps the eleven token
types onto paragraph styles (`Heading 1–3`, `List Bullet`, `List Number`, with depth suffixes),
run flags for bold and italic, and `add_break()` for a hard break. An **import-graph test** asserts
no module in `infrastructure/export/` both imports `docx` and imports the HTML module.

Two consequences, both deliberate:

- A hostile token **cannot reach `python-docx`** either, because the walker consumes the *normalized*
  stream. One normalization protects three walkers.
- **Links become `text (url)`, not real hyperlinks.** `python-docx` exposes no hyperlink API without
  hand-built OXML, and a CV's links are few. The plain-text rendering already had to make the same
  choice, so the two agree by accident of having the same input rather than by a shared constant.

### 5. One stylesheet, one constant, in one module

A4, 18 mm margins, `"Liberation Sans", "DejaVu Sans", sans-serif`, 10.5 pt, headings scaled, lists
indented, links underlined — and **no `@import` and no `url()`**, asserted by a grep test. The
family stack names Liberation first and DejaVu second so a box with either renders text rather than
a page of boxes. Phase 3.2's templates replace that one constant; until then a template is not a
setting, because the stylesheet is the other thing WeasyPrint would fetch from.

### 6. HTML is a one-moment intermediate, and it never leaves the adapter's frame

The HTML string exists as a local in `MarkdownDocumentRenderer` for the duration of one
`write_pdf()`. It is not stored, not logged, not returned, not sent to the API, and not named in any
port signature — `DocumentRendererPort.render(markdown, *, document, format) -> bytes` mentions no
HTML, no CSS, no page size, no font, no timeout and no file. **The word "Markdown" in that signature
is the domain's own format** (ADR-0015 §2 made it the ubiquitous language of a document); HTML would
be one adapter's intermediate for one format, and it stays out.

That frame holds three copies of a stranger's CV — the Markdown, the HTML and the bytes — which
makes it the second-worst Sentry hazard in the codebase after 1.3's prompt frame. The `except
Exception` floor therefore logs the exception's **fully-qualified type only** and re-raises **`from
None`**; `Exception`, never `BaseException`, so `asyncio.CancelledError` still cancels. WeasyPrint's
messages quote the offending HTML and CSS and `python-docx`'s quote XML, so **no vendor message is
ever logged**, and `weasyprint` and `fontTools` join `_SILENCED_VENDOR_LOGGERS` — *after* measuring
what they emit, 1.2's `httpx` lesson.

### 7. There is no outbound HTTP here, and ADR-0012's obligations do not attach

ADR-0012 governs a request to a **caller-chosen host**: an allow-listed scheme in a value object,
DNS resolved before connecting, every resolved address judged, an IP-pinned socket, manual
redirects, `trust_env=False`, a streamed byte cap. None of it applies, because **this slice opens no
socket**. The one library that could — WeasyPrint, on an `<img src>`, a `<link>`, an `@import` or an
`@font-face` — is handed a fetcher that refuses every URL before a name is resolved.

That is a stronger position than a guarded egress, and it is why the ADR is worth writing down: the
row is **unreachable by construction, not by policy**. The test asserts *no socket was opened*
(a patched `socket.socket` that raises), not that a fetch failed.

**The day this changes:** a template with a remote font or a remote image. That re-opens ADR-0012 in
full, and this paragraph is the pointer to it.

## Alternatives

- **`txt` = the stored Markdown.** Rejected: it hands a person `## Experience` to paste into a form.
  It is also indistinguishable from the `md` format, which would make one of the four redundant.
- **`txt` strips bullets and numbers too.** Rejected at the gate: a list without its markers is a
  paragraph, and the "plain" in plain text is about *markup*, not about structure.
- **DOCX via HTML** (an HTML→DOCX converter, or hand-built OXML from the sanitized HTML). Rejected:
  a second consumer behind the sanitizer, a new dependency, and a second place for the grammar to
  drift. The token walk is ~120 lines and shares the normalization.
- **A real hyperlink in the DOCX** (hand-built `w:hyperlink` OXML). Rejected for now: hand-written
  OXML for a handful of links on a CV, against `text (url)` which is legible when pasted anywhere.
  Recorded so the next reader knows it was weighed.
- **Trusting the editor's output and skipping `nh3`.** Genuinely tempting, because ADR-0015 §5's
  argument is sound *on the client*. Rejected: the editor is not the only writer — the model is, and
  a future importer might be — and "the parser cannot emit that tag" is a property of today's
  configuration, not of the type. Two locks, and the second one is tested in isolation.
- **Sanitizing with a hand-rolled allow-list instead of `nh3`.** Rejected: HTML sanitization is a
  genre with a CVE history. `nh3` is Rust's `ammonia`, it parses rather than pattern-matches, and it
  takes the allow-list as data.
- **Letting WeasyPrint fetch local files only** (`base_url` pointing at a static asset directory).
  Rejected: it turns "no egress" into "an egress with a policy", which is the whole of ADR-0012 for
  the benefit of a logo we do not have. The stylesheet is a string; fonts come from the system.
- **A per-template stylesheet setting.** Rejected: a URL in a stylesheet is a fetch, so a
  user-editable stylesheet is an egress. Phase 3.2 replaces the constant in code.

## Consequences

- **The PDF matches what the user saw in the editor**, because both apply the same rule — outside
  the grammar is text — and the clamp is a pure function either side can be tested against.
- **Three new runtime dependencies**, each imported by exactly one module: `weasyprint`
  (`export/pdf.py`), `markdown-it-py` (`export/tokens.py`), `nh3` (`export/html.py`). All four
  renderers — those three plus `docx` — go on **both** import-linter forbidden lists, so none can
  ever appear outside `infrastructure/export/`.
- **WeasyPrint needs system libraries and real fonts at runtime.** The import succeeds without them
  and the *first render* fails, in the worker, where nobody is watching. `docker/api/Dockerfile` and
  `.github/workflows/ci.yml` must list the same apt packages, and AC-46 asserts an embedded font and
  extracted text so a page of boxes fails as a red test rather than as a PDF a human has to open.
- **Every render runs in `asyncio.to_thread` under `asyncio.wait_for`**, in both processes. In the
  worker that is what makes a timeout enforceable at all; in the API it is what keeps even the cheap
  inline parse off the loop (CLAUDE.md's "including the ones that aren't real work" — 1.1's DOCX
  sniff cost 374 ms). The timeout is chosen by `format.delivery`, so one adapter serves both.
- **The thread outlives a timeout.** `wait_for` cancels the await, not the thread; the job records
  `render_timed_out` and the runaway thread is the hard time limit's problem (and
  `--max-tasks-per-child`'s). ADR-0009 already carries this note for extraction.
- **The output cap (20 MiB) is a bound on a renderer bug, not on a user.** A 20,000-character
  document cannot approach it; it exists so that a pathological token stream cannot fill the volume.
- **The grammar, the allow-list, the three schemes, the filenames and the media types are not
  settings.** A knob on an allow-list is an off switch.
- **Nothing from the document is logged at any stage.** The adapter emits `export.render_started`
  (`document`, `format`, `character_count`), `export.render_succeeded` (`format`, `byte_size`,
  `duration_ms`), `export.render_failed` (`format`, `reason`, `error_type`, `duration_ms`) and
  `export.url_fetch_refused` (**the scheme only** — a URL the user typed can carry their name).
