# ADR-0044: A base CV's text keeps its lines, and a DOCX's carries its headings, bold and bullets as Markdown, read only by a model

- **Status:** Accepted
- **Date:** 2026-11-10
- **Relates to:** ADR-0009 (**amended** the same day: its "plain text" now means the text this ADR
  describes; extraction still runs inline in a thread, under the same timeout and the same
  `except Exception` floor), ADR-0004 (**amended** the same day: a flat tailored CV is refused on
  receipt, the other half of this slice's fix), ADR-0015 §2 and ADR-0017 §2 (**amended** the same day:
  a soft break is a line break, which is how the lines this ADR keeps reach the screen and the files),
  ADR-0040 (the fit score's own normalization, which this ADR does not change), ADR-0002 (`re` is the
  standard library, so the domain stays pure). Supersedes nothing. The number 0039 is held for a backups
  decision that has not landed, so its absence on disk is intended.

## Context

On 2026-11-10 the owner reported two things after 5.2's production manual test: a tailored CV
sometimes arrives as one long paragraph, and the tailored CV should keep the headings and bold of the
CV that was uploaded. Reading the code found one cause under both:

1. **Every stored base CV is one line.** `ExtractedText.__post_init__` runs `" ".join(value.split())`.
   The PDF and DOCX extractors join pages and paragraphs with `\n`, and the value object throws every
   one of them away. The model has never seen a CV's lines, so it has had nothing to keep. The eval
   corpus has lines on disk, and the loader builds an `ExtractedText`, so the eval never tested this
   either.
2. **No person ever sees a base CV's text.** The intake API returns `character_count` only. The text
   goes to the tailoring prompt, the fit-score prompt (through `CvForScoring`, which collapses
   whitespace itself) and the admin views, which show it raw with `pre-wrap`. **Nothing renders it as
   Markdown.**
3. **A DOCX knows its headings and bold, and the extractor ignores them.** `paragraph.text` drops the
   paragraph's style and the runs' bold. A PDF, by contrast, gives `pypdf` text with no reliable bold
   or heading data; recovering them from glyph font names would be a guessing heuristic.
4. **Rows already stored are one line**, and nothing re-extracts them: the files are there, but a
   re-extraction is a migration of user data that this slice does not need.

The production evidence (counts only, two prompt-v4 runs from DOCX CVs, both base CVs with 0
newlines) is in the slice's spec. One run had lines that the editor and exports joined (the soft-break
half, ADR-0015 and ADR-0017's amendments). The other had none at all (the flat half, ADR-0004's
amendment).

## Decision

**1. `ExtractedText` keeps lines.** It is still the one place the shape of a CV's text is decided, and
that is where the fix goes. Normalization, in order:

- every line boundary `str.splitlines()` recognises (`\r\n`, `\r`, `\x0b`, `\x0c`, `\x1c`–`\x1e`,
  `\x85`, `\u2028`, `\u2029`) becomes `\n`;
- inside a line, each run of whitespace becomes one space, and the line is stripped;
- two or more blank lines in a row become one;
- leading and trailing blank lines go.

It is **idempotent**, so a value stored before this ADR, which is one line, loads unchanged. The floor
(200 non-whitespace characters), `EmptyExtraction`, `ExtractedTextTooShort` and `character_count =
len(value)` are unchanged; `character_count` now counts newlines. The normalization and the
non-whitespace count stay C-level (`splitlines`, `re.sub`, `str.count`), because they run on every
`BaseCv` load, on the event loop: a 250,000-character, 25,000-line text constructs in 25 ms or less.
**Measured at `/verify` (2026-11-11, api container, median of 21, twice): 5.6–6.0 ms** (max 9.2 ms).
No mutation broke the budget: a per-character generator added on top took 15.7–16.2 ms, and the
whole normalization rewritten per character 18.7–19.4 ms. The owner accepted the budget as held
(option c): 2.2's regression costs about 10 ms at this size, so 25 ms was never tight enough to catch
it. A future change that needs a guard here measures a ratio against a per-character version, not a
wall-clock bound.

**2. A DOCX is written as Markdown, one block per paragraph.**

| python-docx | Markdown |
|---|---|
| style `Title` | `# ` |
| style `Heading n` | `#` × min(n, 6), then a space |
| style name starting `List Bullet` or `List Number`, or exactly `List Paragraph` | `- ` |
| a run whose `bold is True` | `**…**`, adjacent bold runs merged first, the markers hugging the stripped text, its spaces outside |
| a paragraph with no non-whitespace text | nothing: never a lone `#` or `****` |

Blocks are separated by a blank line, and two consecutive list items by one `\n`. The running
character cap (`max_extracted_characters`) counts the emitted Markdown, markup included, and over it is
still `text_too_long`. PDF and TXT get no markup: they keep the lines their library returns, because
decision 1 stopped removing them.

**Four limits, chosen and stated:**

- **Inherited bold is invisible.** `run.bold` is `None` when a character or paragraph style makes the
  text bold, and resolving the style chain is more code than the report asked for. A CV that bolds its
  headings through a style shows them unbolded. Only `True` counts.
- **Bold is not always valid CommonMark emphasis.** The markers hug the text, so a bold stretch that
  starts or ends with punctuation and touches a letter (`**(Lead)**er`) is not a valid close, and bold
  text holding a `*` gives `**5***`. Harmless while only a model reads it (decision 4), and the eval's
  bold-span count may miss such a span. The trigger below covers it too.
- **Every list is `- `.** python-docx does not expose numbering formats publicly, and reading
  `_p.pPr.numPr` is private API.
- **Tables, text boxes, headers and footers are not extracted**, as before this ADR. Many CV templates
  lay out with a table, so this is a real gap. It is a separate slice, not a clause here.

**3. Formatting is best-effort, on a floor.** The Markdown pass sits in its own `try … except
Exception`. If reading any style or run raises (anything other than the adapter's own
`CvExtractionFailed`, which is re-raised), the whole document is extracted the plain way, paragraph
text per block, and one `cv_extraction.docx_formatting_fallback` line is logged with exactly
`content_type`, `size_bytes` and `error_type` (the qualified class name, never the message). The upload
succeeds. ADR-0009's `except Exception` floor and its `extractor_error` stay unchanged beneath both
passes: formatting is extra, and losing it must never cost the upload.

**4. Nothing is escaped.** A literal `*`, `_`, `#`, `` ` ``, `[` or `\` in a DOCX's text passes through
(`5* rating`, `snake_case`, `C# / .NET`). The reader is a model, which reads `C# / .NET` as a person
would, and nothing renders the text as Markdown (context 2). Escaping would put backslashes in front of
the model, which it may copy into the tailored CV that a person does see.

**5. No format column, and old and new rows mix.** `intake_base_cv` gains no `plain` / `markdown`
marker and no migration. No consumer would branch on it: an old row is one line, a new PDF or TXT row
is plain lines, and a new DOCX row is Markdown, and all three are valid input to a prompt that says the
CV block may be Markdown or plain lines (prompt v5). A CV's fit-score fingerprint (ADR-0040) changes
only when its extracted text does, so an old row keeps its scores, and a DOCX uploaded after this ADR
is a new pair, as a different extraction already was.

**The trigger.** The first slice that renders a base CV's text as Markdown, to a person, must revisit
decisions 4 and 5 before it ships: escaping, because `snake_case` and a stray `*` will then render, and
the mixed rows, because an old one-line row and a new Markdown row will then look different side by
side. This paragraph is where to start.

## Alternatives

- **Emit Markdown from the DOCX extractor and leave `ExtractedText` alone.** Does nothing: the value
  object would flatten the Markdown to one line as it flattens everything. This is the trap the slice
  found, and why the domain change comes first.
- **Keep the lines only in the prompt** (re-read the file at tailoring time). Two sources of truth for
  one CV's text, and a file read in the worker for every run. Rejected.
- **Escape Markdown characters now.** Defends a renderer that does not exist, at a cost to the reader
  that does (decision 4). Rejected until the trigger.
- **A format column.** A column no code reads is a migration bought for a future that may not want it.
  Rejected (decision 5).
- **Formatting from PDFs** via font names, or a second library (pdfplumber, PyMuPDF). The first is a
  heuristic; the second is a new dependency with its own ADR. Not now.
- **Re-extract stored base CVs.** Would give old rows lines, and is a bulk rewrite of registered users'
  data with a crash window. Not in this slice. If the after-release counts show flat output from old
  CVs, a re-extraction CLI is a follow-up.

## Consequences

- **The model sees the CV's structure for the first time**, and prompt v5 asks it to keep the headings,
  the bold and a line per header fact. Whether it does is measured by `make eval`, not by a test.
- **A saved CV uploaded before this ADR still tailors from one line.** Re-uploading the file fixes it.
  The UI says nothing about it.
- **A new upload's `character_count` can differ** from the same file's count before this ADR: blank
  lines and markup count. The API contract is otherwise unchanged.
- **Markup makes a DOCX a few percent longer.** `llm_max_cv_characters` (25,000) can refuse a CV near
  the limit slightly earlier, through the adapter's existing pre-flight refusal: the run fails as
  `inputs_too_large` and the credit is refunded (ADR-0042).
- **The admin CV page shows raw Markdown** for a new DOCX: `#` and `**` as characters. The admin is
  the one reader who sees the text, and seeing exactly what the model saw is the right thing for that
  reader.
- **The 200-character floor now counts a DOCX's markup.** The rule is unchanged, but `#`, `**` and
  `- ` are non-whitespace, so a DOCX with about 190 visible characters and a few headings or bold spans
  now passes `CvTextTooShort` and `ExtractedTextTooShort`. Accepted (owner, 2026-11-11 `/verify`): the
  floor was always provisional (OQ-9 of 1.1), and a CV that short fails on quality, not safety. Count
  the floor on the plain text if it is ever made precise.
- **ADR-0009's 3 s p95**, re-timed at `/verify` (2026-11-11, dev stack, 20 uploads each after 3
  warm-ups, a fresh guest session per upload): `sample.docx` p50 0.027 s, **p95 0.047 s**; a formatted
  DOCX (title, 16 headings, bold, 48 bullets, 4,546 characters) p50 0.037 s, **p95 0.060 s**. The
  extra work is a walk over runs already in memory.
- **No new PII and no new destination.** The text goes where it went before, with its lines. The new
  log line carries a class name, and no event's field set changes.
