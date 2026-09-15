import { Editor } from '@tiptap/core';
import { describe, expect, it } from 'vitest';

import {
  documentExtensions,
  documentHeadingLevels,
  documentLinkProtocols,
  documentMarkNames,
  documentNodeNames,
  isLinkSchemeAllowed,
} from './schema';

/**
 * F12 — AC-28: the editor's schema is exactly the document grammar, discovered against a **real**
 * `@tiptap/core` `Editor` built from `documentExtensions` (headless — `Editor` defaults to a
 * detached `document.createElement('div')` when no `element` is given, measured against 3.31.3's
 * `Editor.ts`, so nothing here needs to be mounted into the DOM). Test-after by design: the schema
 * is *configuration*, so the assertion is pinned against what TipTap actually resolves it to, not
 * against a hand-copied list of what the config "should" produce.
 *
 * The two name-set assertions use **equality on sorted arrays**, not `toContain` /
 * `expect.arrayContaining` — the whole point of AC-28 is that an extension added later (which
 * widens the *actual* set) is a red test, not a silent pass because the old members are still
 * present.
 */

function buildEditor(): Editor {
  return new Editor({ extensions: [...documentExtensions] });
}

function findExtension(
  editor: Editor,
  name: string,
): { readonly options: Record<string, unknown> } {
  const extension = editor.extensionManager.extensions.find((candidate) => candidate.name === name);
  if (extension === undefined) {
    throw new Error(`no "${name}" extension found in the resolved extension list`);
  }
  // `Extension<Options = any>`'s `options` is `any` by default (measured in `Extension.ts`), so it
  // is already structurally assignable to `Record<string, unknown>` without a cast — asserting one
  // is what the lint rule below is refusing.
  return extension;
}

describe('AC-28: the schema is exactly the document grammar', () => {
  it('the schema node names equal documentNodeNames, as sets', () => {
    const editor = buildEditor();

    expect(Object.keys(editor.schema.nodes).sort()).toEqual([...documentNodeNames].sort());
  });

  it('the schema mark names equal documentMarkNames, as sets', () => {
    const editor = buildEditor();

    expect(Object.keys(editor.schema.marks).sort()).toEqual([...documentMarkNames].sort());
  });

  it('the heading extension allows exactly levels 1, 2 and 3', () => {
    const editor = buildEditor();

    expect(findExtension(editor, 'heading').options['levels']).toEqual([...documentHeadingLevels]);
  });

  it('the link extension has openOnClick, autolink and linkOnPaste all off', () => {
    const editor = buildEditor();
    const link = findExtension(editor, 'link');

    expect(link.options['openOnClick']).toBe(false);
    expect(link.options['autolink']).toBe(false);
    expect(link.options['linkOnPaste']).toBe(false);
  });

  it('documentLinkProtocols names exactly http, https and mailto', () => {
    expect([...documentLinkProtocols].sort()).toEqual(['http', 'https', 'mailto']);
  });

  describe('the link scheme guard — the runtime check, not the documentation list', () => {
    it.each(['http://example.com', 'HTTP://example.com', 'https://example.com', 'mailto:a@b.com'])(
      'allows %s',
      (url) => {
        expect(isLinkSchemeAllowed(url)).toBe(true);
      },
    );

    it.each([
      ['ftp scheme', 'ftp://example.com'],
      ['tel scheme', 'tel:+15551234567'],
      ['data scheme', 'data:text/html,hi'],
      ['protocol-relative', '//example.com/path'],
      ['bare host, no scheme', 'example.com'],
      ['javascript scheme', 'javascript:alert(1)'],
    ])('refuses a %s href (%s)', (_label, url) => {
      expect(isLinkSchemeAllowed(url)).toBe(false);
    });

    it("the extension's own isAllowedUri option is wired to the same guard", () => {
      const editor = buildEditor();
      const isAllowedUri = findExtension(editor, 'link').options['isAllowedUri'] as
        ((url: string) => boolean) | undefined;

      expect(typeof isAllowedUri).toBe('function');
      expect(isAllowedUri?.('ftp://example.com')).toBe(false);
      expect(isAllowedUri?.('tel:+15551234567')).toBe(false);
      expect(isAllowedUri?.('https://example.com')).toBe(true);
      expect(isAllowedUri?.('mailto:a@b.com')).toBe(true);
    });
  });
});
