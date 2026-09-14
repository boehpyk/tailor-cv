/**
 * The Markdown bridge — the only path between the wire (Markdown, ADR-0015 §2) and the editor
 * (a ProseMirror document under `schema.ts`'s grammar). **There is no HTML path**: the model's text
 * is never handed to `innerHTML`, never to `setContent` as an HTML string, never to a `<div>`. It is
 * tokenized by markdown-it with `html: false`, so a `<script>` in the text is a text token, and
 * the tokens are mapped onto the schema's nodes, so anything the grammar cannot name cannot exist
 * (AC-27, E-13).
 *
 * `createBridge` is the **testing seam** (AC-35): a caller can inject a `MarkdownParser` that
 * throws, so the run page's fallback can be exercised without inventing a malformed input the real
 * parser happens to choke on. The module-level `parseMarkdown` / `serializeMarkdown` are the
 * default bridge bound to the document schema, for the hooks.
 *
 * Skeleton (F8): signatures and types only. F10a builds the parser and serializer.
 */
import type { Node, Schema } from '@tiptap/pm/model';
import type { MarkdownParser } from 'prosemirror-markdown';

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

/* eslint-disable @typescript-eslint/no-unused-vars -- skeleton: every parameter is read in F10a */
export function createBridge(_schema: Schema, _opts: MarkdownBridgeOptions = {}): MarkdownBridge {
  return {
    parseMarkdown(_text: string): Node {
      throw new Error('not implemented');
    },
    serializeMarkdown(_doc: Node): string {
      throw new Error('not implemented');
    },
  };
}

/** The default bridge's parser, bound to the document schema. */
export function parseMarkdown(_text: string): Node {
  throw new Error('not implemented');
}

/** The default bridge's serializer, bound to the document schema. */
export function serializeMarkdown(_doc: Node): string {
  throw new Error('not implemented');
}
/* eslint-enable @typescript-eslint/no-unused-vars */
