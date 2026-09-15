import { EditorContent, useEditor } from '@tiptap/react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { documentExtensions } from '../schema';
import { parseMarkdown, serializeMarkdown } from './bridge';
import {
  javascriptLinkFixtureMarkdown,
  markdownFixtureCorpus,
  normalizationFixtureExpected,
  normalizationFixtureMarkdown,
  rawHtmlFixtureMarkdown,
  selfDescribingLinkFixtureMarkdown,
} from './fixtures';

import type { Node } from '@tiptap/pm/model';

/**
 * F9 RED — the Markdown bridge (feature-spec AC-27, AC-29; technical-plan "The editor" →
 * `markdown/bridge.ts`; E-13).
 *
 * Written against the **spec and the F8 skeleton's real signatures**, not against any
 * implementation: `parseMarkdown` and `serializeMarkdown` (`bridge.ts`) both `throw new
 * Error('not implemented')` today, so every assertion below fails on that thrown error rather than
 * on an `ImportError` — the functions exist, are exported, take the real parameters, and are
 * exercised for real; they just do not work yet (the same shape as a backend `NotImplementedError`
 * red, CLAUDE.md's tiered-TDD table). F10a is expected to make every one of these pass without
 * touching a test.
 */

/**
 * The minimal harness AC-27 needs: a **real** TipTap editor built from the real grammar
 * (`schema.ts`'s `documentExtensions`) and fed by the real bridge's `parseMarkdown` — exactly the
 * combination F10b's finished `useDocumentEditor` will use. Deliberately not `useDocumentEditor`
 * itself: that hook's F8 skeleton seeds a single plain-text paragraph and never calls the bridge (its
 * own docstring says so), so a hostile-content test through it would prove only that TipTap can hold
 * one big text node — nothing about the bridge or the schema, which is what AC-27 is about.
 */
function HostileDocument({ markdown }: { readonly markdown: string }): React.JSX.Element {
  const doc = parseMarkdown(markdown);
  const editor = useEditor({ extensions: [...documentExtensions], content: doc.toJSON() });
  return <EditorContent editor={editor} />;
}

/**
 * Walks a parsed document the way a reader who cares about *marks*, not markup, must: `doc.
 * descendants` visits every node, and each node's `marks` array is the ProseMirror-native place a
 * `link` mark's `href` lives — never the DOM, and never the serialized string. This is what makes
 * the MAJOR 1 tests below structural rather than a second string comparison: AC-29's stability test
 * already proves `serialize(parse(s1)) === s1` as strings, and that equality holds even when the
 * string on both sides has already lost the mark (a `<https://…>` autolink that parses back to
 * plain text is stable, just wrong).
 */
function collectLinkMarkHrefs(doc: Node): string[] {
  const hrefs: string[] = [];
  doc.descendants((node) => {
    for (const mark of node.marks) {
      if (mark.type.name === 'link') {
        hrefs.push(String(mark.attrs['href']));
      }
    }
    return true;
  });
  return hrefs;
}

describe('AC-27 — the editor renders hostile Markdown as inert text, never as HTML (E-13)', () => {
  it('shows a <script> element as literal visible text, and creates no <script> element', () => {
    // The obligation is "never through innerHTML" (AC-27's own wording) — spied on directly rather
    // than inferred from the DOM shape, because a `<script>` element absent from `querySelector`
    // could still mean the fragment reached the page some other way that happens not to parse as a
    // script tag.
    const innerHtmlSetter = vi.spyOn(Element.prototype, 'innerHTML', 'set');

    render(<HostileDocument markdown={rawHtmlFixtureMarkdown} />);

    expect(screen.getByText('<script>alert(1)</script>')).toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
    expect(
      innerHtmlSetter.mock.calls.some(
        ([value]) => typeof value === 'string' && value.includes('alert(1)'),
      ),
    ).toBe(false);

    innerHtmlSetter.mockRestore();
  });

  it('shows an <img onerror> element as literal visible text, and creates no <img> element', () => {
    render(<HostileDocument markdown={rawHtmlFixtureMarkdown} />);

    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  it('never renders a link whose href begins javascript: for a [x](javascript:...) fixture', () => {
    render(<HostileDocument markdown={javascriptLinkFixtureMarkdown} />);

    const hrefs = Array.from(document.querySelectorAll('a')).map(
      (anchor) => anchor.getAttribute('href') ?? '',
    );
    expect(hrefs.some((href) => href.toLowerCase().startsWith('javascript:'))).toBe(false);
  });
});

describe('AC-29 — the Markdown bridge round-trips stably', () => {
  it.each(markdownFixtureCorpus)(
    'serialize(parse(serialize(parse(md)))) equals serialize(parse(md)) for: $name',
    ({ markdown }) => {
      const firstPass = serializeMarkdown(parseMarkdown(markdown));
      const secondPass = serializeMarkdown(parseMarkdown(firstPass));

      expect(secondPass).toBe(firstPass);
    },
  );

  it('normalizes __bold__ to **bold** on the way through — the fact the "no PUT on open" test relies on', () => {
    const serialized = serializeMarkdown(parseMarkdown(normalizationFixtureMarkdown));

    expect(serialized.trim()).toBe(normalizationFixtureExpected.trim());
    // The whole point: normalization happened, so a naive "dirty if md !== original" check would
    // wrongly mark a freshly opened document as edited.
    expect(serialized.trim()).not.toBe(normalizationFixtureMarkdown.trim());
  });
});

/**
 * MAJOR 1 (/verify slice 1.4) — a link whose text equals its `href` must still be a `link` mark
 * after one round trip through the wire format, not merely a stable string.
 *
 * `defaultMarkdownSerializer.marks.link`'s `open`, reused as-is by `createSerializer` in
 * `bridge.ts`, emits CommonMark autolink syntax (`<https://example.com/x>`) whenever a link's text
 * equals its `href`. That is a legitimate shorthand for the *default* prosemirror-markdown parser,
 * whose `autolink` rule reads it straight back into a `link` mark — but this bridge's tokenizer
 * enables only the rules `GRAMMAR_RULES` names (`bridge.ts`), and `autolink` is not one of them
 * (ADR-0015 §2 names `[text](href)` as the grammar's one link form). So the very next parse reads
 * `<https://example.com/x>` as a bare text token: the mark is gone, silently, on the first save of
 * the kind of line a LinkedIn, GitHub or portfolio URL produces every time its display text is the
 * URL itself.
 *
 * Expected values below come from the grammar, never from running the serializer and recording its
 * answer (CLAUDE.md's rule on what a test may encode): the grammar's one link form is
 * `[text](href)`, so a `link` mark with that `href` surviving the round trip — and no `<http` ever
 * appearing in what the bridge itself writes — is what "the bridge did not lose the link" means.
 */
describe('MAJOR 1 — a [url](url) link survives the Markdown bridge as a mark, not just a string', () => {
  it('keeps a self-describing [url](url) link as a link mark after one round trip', () => {
    const firstPassDoc = parseMarkdown(selfDescribingLinkFixtureMarkdown);
    const serialized = serializeMarkdown(firstPassDoc);

    // The bridge's tokenizer never enables `autolink` (`bridge.ts`'s `GRAMMAR_RULES`), so nothing
    // the bridge itself writes may rely on the next parse understanding `<https://…>` — if it did,
    // this is exactly the syntax that would silently drop the mark.
    expect(serialized).not.toContain('<http');

    const roundTrippedDoc = parseMarkdown(serialized);
    expect(collectLinkMarkHrefs(roundTrippedDoc)).toContain('https://example.com/x');
  });

  it.each(markdownFixtureCorpus)(
    'keeps the same set of link-mark hrefs across one round trip for: $name',
    ({ markdown }) => {
      const beforeHrefs = collectLinkMarkHrefs(parseMarkdown(markdown)).sort();
      const afterRoundTripDoc = parseMarkdown(serializeMarkdown(parseMarkdown(markdown)));
      const afterHrefs = collectLinkMarkHrefs(afterRoundTripDoc).sort();

      expect(afterHrefs).toEqual(beforeHrefs);
    },
  );
});
