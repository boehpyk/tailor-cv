/**
 * The editor's extension list **is** the document grammar (ADR-0015 §2, AC-28).
 *
 * ProseMirror can only hold what its schema names. A `<script>` element, an `<img>` with an
 * `onerror`, a `style` attribute — none of these has a node or mark here, so none of them can enter
 * the document, whether they arrive from the model's Markdown, from a paste, or from the server's
 * copy of a revision. That is the sanitizer-by-schema argument, and it holds only while the schema
 * stays exactly this small. The AC-28 test asserts the two name sets below **with equality**, so
 * adding an extension is a red test rather than a silent widening of what 1.5 must render.
 *
 * **The URL-attribute invariant.** The one thing a schema cannot make unrepresentable is a URL in
 * an attribute the schema *does* allow — which is why `link` is the only node or mark here that
 * carries one, and why its href passes the `Link` extension's URI guard on every render. No other
 * node or mark may carry a URL-valued or free-form attribute: an `Image` node (`src`), a `data-*`
 * attribute on a paragraph, a `style` mark or a `class` attribute each re-opens the sanitizer
 * question that this module closes, and each must be argued for in an ADR, not added here.
 *
 * What is measured, not assumed, about the `Link` extension (3.31.3, read in `node_modules`):
 *
 * - `protocols` is **additive**. The extension's own `isAllowedUri` starts from a built-in list
 *   (`http, https, ftp, ftps, mailto, tel, callto, sms, cid, xmpp`) and appends the option's
 *   entries to it — and its regex also admits a scheme-less destination (`//host`, `/path`, a bare
 *   `example.com`). Naming the three in `protocols` documents the grammar and changes nothing at
 *   runtime; the "and nothing else" half of AC-28 is the **`isAllowedUri` option** below
 *   (`(url, ctx) => boolean`, consulted by `renderHTML`, `parseHTML`, `setLink`, the paste rule
 *   and the autolinker alike), which replaces the built-in guard rather than extending it.
 * - `javascript:` is rejected by the default guard already: the built-in regex admits a scheme
 *   only from the list, and a bare word followed by `:` matches neither the "no scheme" branch nor
 *   the "relative" one. Measured (F8) with `setContent` JSON carrying `javascript:alert(1)`,
 *   `JAVASCRIPT:alert(1)`, a zero-width-space and a newline inside `javascript`, `data:text/html,hi`
 *   and `vbscript:`: every one renders as `href=""` in both `getHTML()` and the live view DOM — the
 *   mark survives with an empty href rather than being dropped — while `ftp://…`, `tel:`,
 *   `//host/path` and a bare `example.com` render verbatim. Through HTML parsing (`parseHTML`'s
 *   `getAttrs`) a `javascript:` link is not parsed at all; its text is kept as plain text.
 * - **The guard is render-time only.** After the JSON `setContent` above, `getJSON()` still holds
 *   `href: "javascript:alert(1)"` on the mark. A Markdown serializer reads mark attrs, not the
 *   rendered DOM, so the bridge refuses the scheme on the way *in* — markdown-it's default
 *   `validateLink` already turns `[x](javascript:…)` into plain text, and `markdown/bridge.ts`
 *   narrows it to these same three schemes rather than rely on the render-time `href=""`. The
 *   same rule at both entrances: the bridge's `validateLink` **is** `isLinkSchemeAllowed`.
 * - `openOnClick`, `autolink` and `linkOnPaste` are the three ways a link is *created or followed*
 *   without the user asking for one; all three are off.
 */
import { Link } from '@tiptap/extension-link';
import { StarterKit } from '@tiptap/starter-kit';

import type { AnyExtension } from '@tiptap/core';

/**
 * The node names the schema must contain, exactly — `doc` is ProseMirror's name for the
 * `Document` extension's top node. Compared with equality against
 * `Object.keys(editor.schema.nodes)` in the AC-28 test.
 */
export const documentNodeNames: ReadonlySet<string> = new Set([
  'doc',
  'paragraph',
  'text',
  'heading',
  'bulletList',
  'orderedList',
  'listItem',
  'hardBreak',
]);

/** The mark names the schema must contain, exactly — see `documentNodeNames`. */
export const documentMarkNames: ReadonlySet<string> = new Set(['bold', 'italic', 'link']);

/** The heading levels the grammar allows: `#`, `##`, `###` and no deeper. */
export const documentHeadingLevels: readonly (1 | 2 | 3)[] = [1, 2, 3];

/**
 * The schemes a link may carry. Passed to `Link.configure` to state the grammar, and the list the
 * AC-28 test reads back from the extension's options.
 */
export const documentLinkProtocols: readonly string[] = ['http', 'https', 'mailto'];

/**
 * The `Link` extension's URI guard, replacing (not extending — see the docstring) its built-in
 * one: an `href` is allowed only if it begins with one of `documentLinkProtocols` followed by a
 * colon. It is an **allow-list on the scheme**, which is what makes the classic evasions fall out
 * rather than be enumerated: `JAVASCRIPT:` fails the case-insensitive membership test, and
 * `java\u200bscript:` never parses as a scheme at all. Whitespace is stripped first only so a
 * destination padded with it is judged on what it is, as the built-in guard does. No scheme-less
 * destination is admitted: a relative link means nothing in a document that is exported to a PDF.
 *
 * Exported so the AC-28 test can call it directly rather than reach into the extension's options.
 */
export function isLinkSchemeAllowed(url: string | null | undefined): boolean {
  if (typeof url !== 'string') {
    return false;
  }
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(url.replace(/\s|\p{White_Space}/gu, ''));
  return scheme?.[1] !== undefined && documentLinkProtocols.includes(scheme[1].toLowerCase());
}

/**
 * The extension list, in full. Every StarterKit member outside the grammar is disabled **by name**
 * — 3.31.3 has 22 option keys and registers all of them by default — so a StarterKit upgrade that
 * adds a new default extension still has to get past the equality test, and so a reader sees what
 * was excluded rather than inferring it from what was kept.
 *
 * Non-schema members kept: `undoRedo` (an editor without Ctrl+Z is not an editor), `listKeymap`
 * (Backspace and Delete behave in lists), `dropcursor` (a drop is a paste, and a paste is parsed
 * through this schema). Non-schema members dropped: `gapcursor` exists to step around leaf blocks
 * such as horizontal rules and code blocks, and there are none; `trailingNode` appends an empty
 * paragraph after a document whose last block is not one, which is a change to the text nobody
 * typed — it would make a document that ends in a list dirty on arrival.
 */
export const documentExtensions: readonly AnyExtension[] = [
  StarterKit.configure({
    // Nodes outside the grammar.
    blockquote: false,
    codeBlock: false,
    horizontalRule: false,
    // Marks outside the grammar.
    code: false,
    strike: false,
    underline: false,
    // StarterKit's own `link` is replaced by the configured one below.
    link: false,
    // Behaviour-only extensions that do not belong here (see the docstring).
    gapcursor: false,
    trailingNode: false,
    // Inside the grammar, narrowed.
    heading: { levels: [...documentHeadingLevels] },
  }),
  Link.configure({
    openOnClick: false,
    autolink: false,
    linkOnPaste: false,
    protocols: [...documentLinkProtocols],
    isAllowedUri: (url) => isLinkSchemeAllowed(url),
  }),
];
