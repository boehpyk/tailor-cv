/**
 * The Markdown fixture corpus for the bridge's safety and round-trip tests (AC-27, AC-29, F9 RED).
 *
 * **Every "expected" value here is written by hand from the document grammar (`schema.ts`) — never
 * captured by running the serializer and pasting its output.** AC-29 itself asserts *stability*
 * (`serialize(parse(serialize(parse(md)))) === serialize(parse(md))`), not a golden string, so most
 * fixtures below carry no expected output at all. `normalizationFixtureMarkdown` is the one
 * exception: the "opening a document does not dirty it" test needs a fixture where
 * `serialize(parse(md)) !== md` is a **certainty**, not an assumption verified by running the code
 * that does not exist yet — so its normalized form is transcribed from CommonMark's canonical
 * rendering of `__strong emphasis__` (`**strong emphasis**`), the one normalization every
 * CommonMark-conformant serializer performs unconditionally: unlike a bullet marker (which
 * `prosemirror-markdown`-style parsers commonly preserve as a stored node attribute), a mark has no
 * "original spelling" to remember, so a strong mark is serialized the same way regardless of which
 * of the two equivalent source delimiters produced it.
 */

export interface MarkdownFixture {
  readonly name: string;
  readonly markdown: string;
}

/**
 * Every grammar element in one document: headings 1–3, a paragraph, bold, italic, a link, a hard
 * break (CommonMark's backslash-before-newline form — chosen over trailing spaces, which a linter
 * or an editor can silently trim from a fixture file), a bullet list and an ordered list.
 */
export const grammarFixtureMarkdown = `# Heading one

## Heading two

### Heading three

A paragraph with **bold** and *italic* text, and a [link](https://example.com/apply).

- First bullet
- Second bullet

1. First step
2. Second step

A line with a hard break\\
that continues on the next line.
`;

/**
 * Raw HTML in the source. There is no HTML path (E-13): a `<script>` and an `<img onerror>` must
 * come through as inert text, never as elements.
 */
export const rawHtmlFixtureMarkdown = `# Profile

<script>alert(1)</script>

<img src=x onerror=alert(1)>

Regular paragraph text after the hostile lines.
`;

/** A link whose scheme is refused outright — never a followable, scripted `href` (E-13, AC-27). */
export const javascriptLinkFixtureMarkdown = `Click [x](javascript:alert(1)) to continue reading.`;

/** A model-shaped CV — the kind of document the LLM actually returns (ADR-0015 §2). */
export const modelCvFixtureMarkdown = `# Jordan Rivera

## Experience

**Senior Backend Engineer**, Acme Corp

- Led the migration to a *hexagonal* architecture
- Reduced p95 latency by 40% across the payments service

**Backend Engineer**, Widgets Inc

- Owned the on-call rotation for the billing pipeline

## Education

### BSc Computer Science

[University website](https://example.edu)
`;

/**
 * A link whose text is identical to its `href` — the common LinkedIn/GitHub/portfolio line shape
 * (`[https://example.com/x](https://example.com/x)`), and MAJOR 1's fixture (/verify slice 1.4).
 * `defaultMarkdownSerializer.marks.link`'s `open` emits CommonMark autolink syntax
 * (`<https://example.com/x>`) whenever a link's text equals its `href` — a legitimate CommonMark
 * shorthand the *default* serializer is entitled to use because the *default* parser's `autolink`
 * rule reads it back as a link. This bridge's tokenizer does not enable `autolink`
 * (`bridge.ts`'s `GRAMMAR_RULES`, and ADR-0015 §2's grammar names only `[text](href)`), so the next
 * parse reads `<https://example.com/x>` as plain text: the mark is gone on the very first round
 * trip a user's own profile link would take through the editor.
 */
export const selfDescribingLinkFixtureMarkdown =
  'See [https://example.com/x](https://example.com/x) for more.';

/** A model-shaped cover letter — the second document every succeeded run carries (TR-5). */
export const modelLetterFixtureMarkdown = `# Cover Letter

Dear Hiring Manager,

I am writing to apply for the **Senior Backend Engineer** role at Acme Corp. My experience leading
a *hexagonal* migration maps directly onto the responsibilities in your posting.

1. Ownership of a production payments pipeline
2. A track record of measurable latency work

Sincerely,
Jordan Rivera
`;

/**
 * A source the bridge is certain to normalize on the way through (`__bold__` → `**bold**` —
 * see the module docstring for why this delimiter, and not a bullet marker, is the safe choice).
 * Used by the "opening a document does not make it dirty" test (AC-29), which needs
 * `serialize(parse(md)) !== md` to be a known fact rather than a runtime check against code that
 * does not exist yet.
 */
export const normalizationFixtureMarkdown = `A paragraph with __bold__ text that should normalize.
`;

/** Hand-transcribed, not captured — see the module docstring. */
export const normalizationFixtureExpected = 'A paragraph with **bold** text that should normalize.';

/**
 * A `[label](destination)` shape whose destination has no scheme at all — never a link attempt, and
 * never reachable by the server's `_allow_three_schemes` as anything but "not allowed" (MAJOR 1,
 * `/verify` slice 1.5, mirrored from `api/tests/fixtures/documents/__init__.py`'s
 * `NO_SCHEME_BRACKET_FIXTURE_MARKDOWN`). "$100k" written by an author as "[100k](150k)" is a salary
 * range, not a refused URL, and "[1](note)" is a citation, not an attack — the server's
 * `strip_refused_link_markup` must leave this document byte-identical, which is exactly what it does
 * not do today.
 */
export const noSchemeBracketFixtureMarkdown = `## Compensation

Negotiated salary range [100k](150k) after the offer, cited as [1](note) in the report.
`;

/** The full corpus AC-29's round-trip-stability test iterates. */
export const markdownFixtureCorpus: readonly MarkdownFixture[] = [
  { name: 'every grammar element', markdown: grammarFixtureMarkdown },
  { name: 'raw HTML (script and an onerror image)', markdown: rawHtmlFixtureMarkdown },
  { name: 'a javascript: link', markdown: javascriptLinkFixtureMarkdown },
  { name: 'a model-shaped CV', markdown: modelCvFixtureMarkdown },
  { name: 'a model-shaped cover letter', markdown: modelLetterFixtureMarkdown },
  { name: 'a [url](url) self-describing link', markdown: selfDescribingLinkFixtureMarkdown },
  { name: 'certain normalisation (__bold__ → **bold**)', markdown: normalizationFixtureMarkdown },
  {
    name: 'a non-URL bracket-paren pair (salary range, citation)',
    markdown: noSchemeBracketFixtureMarkdown,
  },
];
