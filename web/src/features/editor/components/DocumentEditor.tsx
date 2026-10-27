import { EditorContent, useEditorState } from '@tiptap/react';

import type { DocumentEditorHandle } from '../hooks/useDocumentEditor';
import type { TailoredDocumentKind } from '@/features/tailoring/types';
import type { Editor } from '@tiptap/core';

export interface DocumentEditorProps {
  readonly handle: DocumentEditorHandle;
  readonly kind: TailoredDocumentKind;
  /** The other tab is open. The instance stays mounted; only the pane leaves the layout (AC-30). */
  readonly hidden: boolean;
}

/** One toolbar control: its accessible name, what it does, and how to tell whether it is on. */
interface ToolbarControl {
  readonly label: string;
  /** What the button shows (web-design-refresh C-6). Decoration: the name is still `label`. */
  readonly glyph: string;
  readonly run: (editor: Editor) => void;
  readonly isActive: (editor: Editor) => boolean;
}

/**
 * The seven formatting controls, in toolbar order. The accessible name is the contract the F12
 * markup test queries by; `aria-pressed` reflects whether the mark or node is active at the cursor.
 * Every command runs through `chain().focus()`, so clicking a button does not leave the caret in
 * the toolbar.
 */
const TOOLBAR_CONTROLS: readonly ToolbarControl[] = [
  {
    label: 'Bold',
    glyph: 'B',
    run: (editor) => editor.chain().focus().toggleBold().run(),
    isActive: (editor) => editor.isActive('bold'),
  },
  {
    label: 'Italic',
    glyph: 'I',
    run: (editor) => editor.chain().focus().toggleItalic().run(),
    isActive: (editor) => editor.isActive('italic'),
  },
  {
    label: 'Heading 1',
    glyph: 'H1',
    run: (editor) => editor.chain().focus().toggleHeading({ level: 1 }).run(),
    isActive: (editor) => editor.isActive('heading', { level: 1 }),
  },
  {
    label: 'Heading 2',
    glyph: 'H2',
    run: (editor) => editor.chain().focus().toggleHeading({ level: 2 }).run(),
    isActive: (editor) => editor.isActive('heading', { level: 2 }),
  },
  {
    label: 'Heading 3',
    glyph: 'H3',
    run: (editor) => editor.chain().focus().toggleHeading({ level: 3 }).run(),
    isActive: (editor) => editor.isActive('heading', { level: 3 }),
  },
  {
    label: 'Bullet list',
    glyph: '•',
    run: (editor) => editor.chain().focus().toggleBulletList().run(),
    isActive: (editor) => editor.isActive('bulletList'),
  },
  {
    label: 'Numbered list',
    glyph: '1.',
    run: (editor) => editor.chain().focus().toggleOrderedList().run(),
    isActive: (editor) => editor.isActive('orderedList'),
  },
];

/**
 * One document's pane — presentational: `EditorContent` under a `role="toolbar"` of seven
 * `<button type="button" aria-pressed>`s.
 *
 * **`aria-pressed` comes from `useEditorState`, not from reading `editor.isActive` in render.**
 * The editor is an instance outside React; a transaction that moves the caret into a heading
 * changes nothing React knows about, so a plain read would be stale until something else
 * re-rendered. `useEditorState` subscribes to the instance's transactions and re-renders this
 * component when the selected slice changes — the same shape as `useSyncExternalStore`, which is
 * what it is built on. The selector returns one boolean per control; `editable` is read the same
 * way so the toolbar disables itself with the editor after a 401 (AC-34).
 *
 * The pane is `aria-busy` while the instance is `null` — the loading state of a document, which
 * `useDocumentEditor` never produces but the handle's type allows.
 */
export function DocumentEditor({ handle, kind, hidden }: DocumentEditorProps): React.JSX.Element {
  const { editor } = handle;
  const toolbar = useEditorState({
    editor,
    selector: ({ editor: current }) =>
      current === null
        ? null
        : {
            editable: current.isEditable,
            active: TOOLBAR_CONTROLS.map((control) => control.isActive(current)),
          },
  });

  return (
    <div
      id={`document-${kind}`}
      role="tabpanel"
      hidden={hidden}
      data-document={kind}
      aria-busy={editor === null}
      className="mx-auto max-w-[794px] rounded-sm bg-white shadow-md"
    >
      {/* Sticky at the top of the sheet: a long CV keeps its toolbar in reach while scrolling. */}
      <div
        role="toolbar"
        aria-label="Formatting"
        className="sticky top-0 z-10 flex flex-wrap gap-1 rounded-t-sm border-b border-slate-200 bg-white px-3 py-2 sm:px-6"
      >
        {TOOLBAR_CONTROLS.map((control, index) => {
          const pressed = toolbar?.active[index] ?? false;
          return (
            <button
              key={control.label}
              type="button"
              aria-pressed={pressed}
              disabled={editor === null || toolbar?.editable === false}
              onClick={() => {
                if (editor !== null) {
                  control.run(editor);
                }
              }}
              className={`inline-flex size-11 items-center justify-center rounded-md text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-50 sm:size-8 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 ${
                pressed ? 'bg-slate-900 text-white' : 'text-slate-700 hover:bg-surface-sunk'
              }`}
            >
              {/* The glyph is what a sighted reader sees; the name stays the label (AC-21). */}
              <span aria-hidden="true">{control.glyph}</span>
              <span className="sr-only">{control.label}</span>
            </button>
          );
        })}
      </div>
      <div className="p-5 sm:p-12">
        <EditorContent editor={editor} />
      </div>
    </div>
  );
}
