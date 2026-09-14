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
} from './fixtures';

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
