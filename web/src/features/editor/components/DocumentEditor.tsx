import { EditorContent } from '@tiptap/react';

import type { DocumentEditorHandle } from '../hooks/useDocumentEditor';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

export interface DocumentEditorProps {
  readonly handle: DocumentEditorHandle;
  readonly kind: TailoredDocumentKind;
  /** The other tab is open. The instance stays mounted; only the pane leaves the layout (AC-30). */
  readonly hidden: boolean;
}

/**
 * The seven formatting controls, in toolbar order. The accessible name is the contract the F12
 * markup test queries by; `aria-pressed` reflects whether the mark or node is active at the cursor.
 */
const TOOLBAR_CONTROLS: readonly string[] = [
  'Bold',
  'Italic',
  'Heading 1',
  'Heading 2',
  'Heading 3',
  'Bullet list',
  'Numbered list',
];

/**
 * One document's pane — presentational: `EditorContent` under a `role="toolbar"` of seven
 * `<button type="button" aria-pressed>`s.
 *
 * Skeleton (F8): the buttons do nothing and are never pressed. F10b wires each to the editor's
 * command and reads its active state.
 */
export function DocumentEditor({ handle, kind, hidden }: DocumentEditorProps): React.JSX.Element {
  return (
    <div hidden={hidden} data-document={kind}>
      <div role="toolbar" className="mb-2 flex flex-wrap gap-1">
        {TOOLBAR_CONTROLS.map((label) => (
          <button
            key={label}
            type="button"
            aria-pressed={false}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm text-slate-800"
          >
            {label}
          </button>
        ))}
      </div>
      <div className="rounded-md border border-slate-300 bg-white px-3 py-2">
        <EditorContent editor={handle.editor} />
      </div>
    </div>
  );
}
