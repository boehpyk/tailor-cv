/**
 * The Markdown bridge — the only path between the wire (Markdown, ADR-0015 §2) and the editor
 * (a ProseMirror document under `schema.ts`'s grammar). **There is no HTML path**: the model's text
 * is never handed to `innerHTML`, never to `setContent` as an HTML string, never to a `<div>`. It is
 * tokenized by markdown-it with `html: false`, so a `<script>` in the text is a text token, and
 * the tokens are mapped onto the schema's nodes, so anything the grammar cannot name cannot exist
 * (AC-27, E-13).
 *
 * **TipTap is ProseMirror.** That is the one fact this file teaches. `prosemirror-markdown`'s
 * parser and serializer know nothing about TipTap; they are configured against a `Schema` and a
 * map of token names to *node names*, and TipTap's node names (`bulletList`, `hardBreak`, marks
 * `bold` and `italic`) are simply different spellings from the ones the default map uses
 * (`bullet_list`, `hard_break`, `strong`, `em`). Re-keying the map is the whole adaptation.
 *
 * **The grammar is enforced twice, on purpose.** markdown-it starts from the `zero` preset —
 * nothing enabled — and switches on exactly the rules the grammar needs (measured against
 * markdown-it 14.3.2's rule tables in `parser_block.mjs` / `parser_inline.mjs`):
 *
 * - block: `heading` (ATX `#`), `list`, `paragraph`;
 * - inline: `text`, `newline` (a soft break, and the two-trailing-spaces hard break), `escape`
 *   (backslash escapes **and** the backslash-newline hard break — CommonMark puts both in one
 *   rule), `emphasis`, `link`;
 * - core: `normalize`, `block`, `inline`, `text_join` — the four the `zero` preset already
 *   carries; `text_join` folds the `text_special` tokens `escape` emits back into text.
 *
 * Everything else — `table`, `fence`, `code`, `blockquote`, `hr`, `html_block`, `html_inline`,
 * `image`, `autolink`, `backticks`, `strikethrough`, `entity`, `reference`, `lheading` — stays
 * off, so a table or a fenced block in the model's output is a paragraph of text. `html: false`
 * would keep raw HTML as text even with `html_block` on; disabling the rule too means the guarantee
 * does not rest on one option. Then the token map names only the grammar's nodes, so a token the
 * tokenizer *could* still emit and the map does not name is an error rather than a silent node —
 * the second fence.
 *
 * **Links are refused on the way in, not only at render.** `schema.ts` records the measurement:
 * the `Link` extension's URI guard is render-time only, and a mark whose `href` is
 * `javascript:alert(1)` survives in the document JSON with an empty rendered `href`. A Markdown
 * serializer reads mark attrs, not the rendered DOM, so the bad scheme would round-trip to the
 * server. `validateLink` below therefore admits `http`, `https` and `mailto` and nothing else —
 * narrower than markdown-it's own deny-list (`javascript`, `vbscript`, `file`, `data`) — and a
 * refused destination makes the whole `[text](dest)` plain text, which is what AC-27's fixture
 * asserts. It is `schema.ts`'s `isLinkSchemeAllowed` — the same function the `Link` extension's
 * `isAllowedUri` option runs for links created *inside* the editor — so the two entrances cannot
 * disagree about a scheme.
 *
 * `createBridge` is the **testing seam** (AC-35): a caller can inject a `MarkdownParser` that
 * throws, so the run page's fallback can be exercised without inventing a malformed input the real
 * parser happens to choke on. The module-level `parseMarkdown` / `serializeMarkdown` are the
 * default bridge bound to the document schema, for the hooks.
 *
 * **Why the hooks pass JSON, not the `Node`, to the editor.** `documentSchema` below and the
 * `Editor`'s own `schema` are built from one extension list, so they are structurally equal — and
 * they are still two `Schema` instances. ProseMirror validates content by `NodeType` identity, so a
 * `Node` parsed under one cannot be inserted under the other. `parseMarkdown(text).toJSON()` is
 * re-instantiated by the editor under its own schema; the serializer, which keys on `type.name`,
 * needs no such care.
 */
import { getSchema } from '@tiptap/core';
import MarkdownIt from 'markdown-it';
import {
  defaultMarkdownSerializer,
  MarkdownParser,
  MarkdownSerializer,
} from 'prosemirror-markdown';

import { documentExtensions, isLinkSchemeAllowed } from '../schema';

import type { JSONContent } from '@tiptap/core';
import type { Node, Schema } from '@tiptap/pm/model';

export interface MarkdownBridgeOptions {
  /** A replacement parser — the AC-35 seam. Defaults to the grammar's markdown-it parser. */
  readonly parser?: MarkdownParser;
}

export interface MarkdownBridge {
  /** Markdown text → a document under `schema`. Throws if the text cannot be parsed. */
  parseMarkdown(text: string): Node;
  /** A document under `schema` → Markdown text. */
  serializeMarkdown(doc: Node): string;
}

/**
 * A parsed document as the editor accepts it — see the module docstring for why JSON crosses the
 * boundary and not the `Node`. `Node.toJSON()` is typed `any` by ProseMirror; its shape is exactly
 * what TipTap's `JSONContent` describes (`type`, `attrs`, `content`, `marks`, `text`), which is
 * the one fact the assertion states.
 */
export function toEditorContent(doc: Node): JSONContent {
  return doc.toJSON() as JSONContent;
}

/** The markdown-it rules the grammar needs, by the names markdown-it 14 gives them. */
const GRAMMAR_RULES: readonly string[] = [
  'heading',
  'list',
  'paragraph',
  'text',
  'newline',
  'escape',
  'emphasis',
  'link',
];

/**
 * A tokenizer that knows the grammar and nothing else. `html: false` is stated even though the
 * `zero` preset already has it off: it is the one option ADR-0015 §2 names, and a reader should
 * find it here without opening the preset.
 */
function createTokenizer(): MarkdownIt {
  const md = new MarkdownIt('zero', { html: false }).enable([...GRAMMAR_RULES]);
  // Called with the destination after markdown-it's own `normalizeLink` (percent-encoding) —
  // the scheme is untouched by that, so the schema's guard applies as written.
  md.validateLink = isLinkSchemeAllowed;
  return md;
}

/** `bullet_list_open` … `list_item_close` → the TipTap-named nodes; `em`/`strong` → the marks. */
function createParser(schema: Schema): MarkdownParser {
  return new MarkdownParser(schema, createTokenizer(), {
    paragraph: { block: 'paragraph' },
    heading: { block: 'heading', getAttrs: (tok) => ({ level: Number(tok.tag.slice(1)) }) },
    bullet_list: { block: 'bulletList' },
    ordered_list: {
      block: 'orderedList',
      getAttrs: (tok) => ({ start: Number(tok.attrGet('start') ?? '1') || 1 }),
    },
    list_item: { block: 'listItem' },
    hardbreak: { node: 'hardBreak' },
    em: { mark: 'italic' },
    strong: { mark: 'bold' },
    link: {
      mark: 'link',
      getAttrs: (tok) => ({ href: tok.attrGet('href'), title: tok.attrGet('title') ?? null }),
    },
  });
}

/**
 * The serializer: `defaultMarkdownSerializer`'s node and mark functions, re-keyed onto TipTap's
 * names, with three deliberate choices of spelling — none of which affects AC-29's stability, all
 * of which decide what the *first* save writes back:
 *
 * - bullets are `-`, the marker the model writes, so a list survives a round trip byte-for-byte;
 * - lists are tight (`tightLists: true`, passed at `serialize` time, which is where the typings
 *   put it): TipTap's list nodes carry no `tight` attribute, so the option decides, and the model
 *   writes tight lists;
 * - `hardBreakNodeName` names TipTap's node, so the serializer's rule about trailing hard breaks
 *   at the end of a block applies to ours.
 */
function createSerializer(): MarkdownSerializer {
  const { nodes, marks } = defaultMarkdownSerializer;
  const heading = nodes['heading'];
  const paragraph = nodes['paragraph'];
  const listItem = nodes['list_item'];
  const hardBreak = nodes['hard_break'];
  const text = nodes['text'];
  const em = marks['em'];
  const strong = marks['strong'];
  const link = marks['link'];
  if (
    heading === undefined ||
    paragraph === undefined ||
    listItem === undefined ||
    hardBreak === undefined ||
    text === undefined ||
    em === undefined ||
    strong === undefined ||
    link === undefined
  ) {
    // `prosemirror-markdown` ships all eight; the check is what lets the re-keying stay typed
    // under `noUncheckedIndexedAccess` without a `!`.
    throw new Error('prosemirror-markdown: the default serializer is missing a grammar entry');
  }

  return new MarkdownSerializer(
    {
      heading,
      paragraph,
      listItem,
      hardBreak,
      text,
      bulletList(state, node) {
        state.renderList(node, '  ', () => '- ');
      },
      orderedList(state, node) {
        const start = typeof node.attrs['start'] === 'number' ? node.attrs['start'] : 1;
        const maxWidth = String(start + node.childCount - 1).length;
        const space = state.repeat(' ', maxWidth + 2);
        state.renderList(node, space, (i) => {
          const label = String(start + i);
          return `${state.repeat(' ', maxWidth - label.length)}${label}. `;
        });
      },
    },
    { italic: em, bold: strong, link },
    { hardBreakNodeName: 'hardBreak' },
  );
}

export function createBridge(schema: Schema, opts: MarkdownBridgeOptions = {}): MarkdownBridge {
  const parser = opts.parser ?? createParser(schema);
  const serializer = createSerializer();
  return {
    parseMarkdown(text: string): Node {
      return parser.parse(text);
    },
    serializeMarkdown(doc: Node): string {
      // `tightLists` is a per-call option in prosemirror-markdown's typings, not a constructor one.
      return serializer.serialize(doc, { tightLists: true });
    },
  };
}

/**
 * The grammar's schema, built once from the extension list. Every hook that seeds an editor goes
 * through this bridge; see the module docstring for why what crosses into the editor is JSON.
 */
export const documentSchema: Schema = getSchema([...documentExtensions]);

const defaultBridge = createBridge(documentSchema);

/** The default bridge's parser, bound to the document schema. */
export function parseMarkdown(text: string): Node {
  return defaultBridge.parseMarkdown(text);
}

/** The default bridge's serializer, bound to the document schema. */
export function serializeMarkdown(doc: Node): string {
  return defaultBridge.serializeMarkdown(doc);
}
