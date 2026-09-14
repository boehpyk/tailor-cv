import { DocumentEditor } from './DocumentEditor';
import { DocumentTabs } from './DocumentTabs';
import { SaveIndicator } from './SaveIndicator';
import { useDocumentAutosave } from '../hooks/useDocumentAutosave';
import { useDocumentEditor } from '../hooks/useDocumentEditor';

import type { TailoringRun } from '@/features/tailoring/types';

export interface DocumentWorkspaceProps {
  /** A `succeeded` run — both documents present (TR-5). The caller keys this component on `run.id`. */
  readonly run: TailoringRun;
}

/**
 * The editor over one run — the **container** for both documents (technical plan, "The editor").
 *
 * Both `useDocumentEditor`s and both `useDocumentAutosave`s are called at the top level,
 * unconditionally: hooks cannot be conditional, and a succeeded run has both documents, so there
 * is nothing to condition on. Both `DocumentEditor`s are rendered for the life of the page and the
 * URL's `:document` decides which is `hidden` (AC-30) — the inactive instance keeps its document,
 * its undo history and its unsaved edits, and switching tabs is a visibility change, not a mount.
 *
 * `tailored_cv` and `cover_letter` are `null` while a run is in flight (the polling contract), and
 * the type says so. This component is only rendered on `succeeded`, where both are set; `?? ''`
 * states that in a way the compiler accepts without a `!`, and an empty editor is what a reader
 * would see if the claim were ever wrong.
 *
 * Skeleton (F8): both editors visible, the CV's indicator, no footer, no error boundary, no
 * `beforeunload`. F10b, F10c and F10d fill those in, in that order.
 */
export function DocumentWorkspace({ run }: DocumentWorkspaceProps): React.JSX.Element {
  const cv = useDocumentEditor(run.tailored_cv ?? '', { editable: true });
  const coverLetter = useDocumentEditor(run.cover_letter ?? '', { editable: true });
  const cvState = useDocumentAutosave(run.id, 'cv', cv);
  const coverLetterState = useDocumentAutosave(run.id, 'cover_letter', coverLetter);

  return (
    <div>
      <DocumentTabs runId={run.id} states={{ cv: cvState, cover_letter: coverLetterState }} />
      <SaveIndicator state={cvState} />
      <DocumentEditor handle={cv} kind="cv" hidden={false} />
      <DocumentEditor handle={coverLetter} kind="cover_letter" hidden={false} />
    </div>
  );
}
