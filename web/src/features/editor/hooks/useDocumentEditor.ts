import { useEditor } from '@tiptap/react';
import { useEffect, useMemo, useRef } from 'react';

import { parseMarkdown, serializeMarkdown, toEditorContent } from '../markdown/bridge';
import { documentExtensions } from '../schema';

import type { Editor } from '@tiptap/core';
import type { JSONContent } from '@tiptap/core';

/**
 * What a component gets back from `useDocumentEditor`: the instance for `EditorContent` and the
 * toolbar, plus the three questions the autosave asks of it. Typed explicitly, so the autosave
 * hook can be tested against a hand-built handle without a real editor.
 */
export interface DocumentEditorHandle {
  /**
   * The TipTap instance. Measured against `@tiptap/react` 3.31.3: `useEditor` creates it
   * synchronously on the first render (`immediatelyRender` defaults to `true` outside SSR), so the
   * hook itself never hands out `null`. The type keeps `null` because `EditorContent` accepts it and
   * a hand-built handle in a test may have none; a caller that needs the instance checks, never
   * asserts.
   */
  readonly editor: Editor | null;
  /** The document as Markdown — what a `PUT` body's `content` carries. */
  serialize(): string;
  /** Whether the document differs from what it was seeded with (AC-29: opening is not dirtying). */
  isDirty(): boolean;
  /** Replace the document from server text — "Load the latest version" (AC-33). Imperative on purpose. */
  reseed(text: string): void;
}

export interface DocumentEditorOptions {
  /** `false` after the session expires (AC-34): the text stays on screen, read-only. */
  readonly editable: boolean;
}

/**
 * One document's editor instance — the hook that owns a TipTap `Editor` for the life of the
 * component that calls it.
 *
 * **Seeded once, through the bridge.** The seed is `parseMarkdown(text)` — never `text` itself,
 * because a string handed to TipTap's `content` is parsed **as HTML**, and this codebase has no HTML
 * path for a document (E-13). It crosses into the editor as JSON, for the reason `bridge.ts`'s
 * docstring gives (two structurally equal `Schema` instances are still two instances). It is parsed
 * in a lazily-initialised ref rather than in `useState`: nothing here is state — the document lives
 * in the ProseMirror instance and nowhere else, never copied into React, and the two editors are
 * never lifted above their tabs (AC-30) — and rather than inline, because `useEditor` reads
 * `content` at creation only and re-parsing a CV on every render would be work whose result is
 * thrown away. A later change to `text` does not re-seed, and nothing recreates the instance
 * because a prop changed: a different run is a different instance because the parent keys on
 * `runId` (technical plan, "The editor").
 *
 * **The dirty baseline is the serialization at seed time, not the seed text** (AC-29). The bridge
 * normalises on the way through (`__bold__` becomes `**bold**`), so `serialize() !== text` is true
 * for a document nobody has touched; comparing against what the editor's own document serialised
 * to the moment it was created is what makes "opening is not dirtying" hold. It lives in a ref,
 * not state: it is a fact taken at one moment, read imperatively by `isDirty`, and must survive
 * re-renders without causing one. `reseed` moves it — the server's text is the new "what I was
 * given" — and replaces the document with `emitUpdate: false`, because the `update` event means
 * "the person typed" to the autosave that listens for it, and a programmatic replace is not that.
 *
 * Two things measured in `@tiptap/react` 3.31.3 (`EditorInstanceManager.onRender`): with no `deps`
 * the instance is kept and changed options are pushed with `setOptions`, which (a) never
 * re-applies `content` — the seed really is once — and (b) explicitly **preserves the instance's
 * current `editable`**, so a changed `opts.editable` reaches the instance only through the
 * `useEffect` below, which calls `editor.setEditable` — synchronizing an instance outside React,
 * the legitimate kind. `DocumentWorkspace` lowers `editable` on both instances the same way once a
 * save answers 401 (AC-34); nothing raises it again for those instances.
 */
export function useDocumentEditor(text: string, opts: DocumentEditorOptions): DocumentEditorHandle {
  const seed = useRef<JSONContent | null>(null);
  seed.current ??= toEditorContent(parseMarkdown(text));

  const editor = useEditor({
    extensions: [...documentExtensions],
    content: seed.current,
    editable: opts.editable,
  });

  const baseline = useRef<string | null>(null);
  baseline.current ??= serializeMarkdown(editor.state.doc);

  useEffect(() => {
    editor.setEditable(opts.editable);
  }, [editor, opts.editable]);

  return useMemo<DocumentEditorHandle>(
    () => ({
      editor,
      serialize: () => serializeMarkdown(editor.state.doc),
      isDirty: () => serializeMarkdown(editor.state.doc) !== baseline.current,
      reseed: (next: string) => {
        editor.commands.setContent(toEditorContent(parseMarkdown(next)), { emitUpdate: false });
        baseline.current = serializeMarkdown(editor.state.doc);
      },
    }),
    [editor],
  );
}
