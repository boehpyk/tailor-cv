import { useEditor } from '@tiptap/react';
import { useMemo } from 'react';

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
 * Seed a single paragraph holding `text` verbatim — a **plain-text** seed, deliberately not the
 * bridge (F10b replaces this with `parseMarkdown`). Built as JSON rather than passed as a string,
 * because a string handed to TipTap's `content` is parsed **as HTML**, and this codebase has no
 * HTML path for a document (E-13). An empty text is an empty paragraph: ProseMirror forbids an
 * empty text node.
 */
function plainParagraph(text: string): JSONContent {
  return {
    type: 'doc',
    content: [{ type: 'paragraph', content: text === '' ? [] : [{ type: 'text', text }] }],
  };
}

/**
 * One document's editor instance — the hook that owns a TipTap `Editor` for the life of the
 * component that calls it.
 *
 * **Seeded once.** `useEditor`'s options are read at creation; a later change to `text` does not
 * re-seed, and nothing recreates the instance because a prop changed — a different run is a
 * different instance because the parent keys on `runId` (technical plan, "The editor"). The
 * document being edited lives in the ProseMirror instance and nowhere else: it is never copied into
 * `useState`, and the two editors are never lifted above their tabs (AC-30).
 *
 * Two things measured in `@tiptap/react` 3.31.3 (`EditorInstanceManager.onRender`) that F10b must
 * respect: with no `deps` the instance is kept and changed options are pushed with `setOptions`,
 * which (a) never re-applies `content` — the seed really is once — and (b) explicitly **preserves
 * the instance's current `editable`**, so flipping `opts.editable` here does nothing after
 * creation. Making the editor read-only on a 401 (AC-34) is `editor.setEditable(false)` from a
 * `useEffect` — synchronizing an instance outside React, the legitimate kind.
 *
 * Skeleton (F8): the seed is plain text, `serialize` is the editor's own `getText()`, `isDirty`
 * is always `false` and `reseed` does nothing. F10b brings the bridge, the seed-time baseline in a
 * ref, and the real `reseed`.
 */
export function useDocumentEditor(text: string, opts: DocumentEditorOptions): DocumentEditorHandle {
  const editor = useEditor({
    extensions: [...documentExtensions],
    content: plainParagraph(text),
    editable: opts.editable,
  });

  return useMemo<DocumentEditorHandle>(
    () => ({
      editor,
      serialize: () => editor.getText(),
      isDirty: () => false,
      // eslint-disable-next-line @typescript-eslint/no-unused-vars -- skeleton: `setContent` in F10b
      reseed: (_next: string) => undefined,
    }),
    [editor],
  );
}
