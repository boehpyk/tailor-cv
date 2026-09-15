import { useEffect } from 'react';
import { Link, useParams } from 'react-router';

import { TailoredDocumentsPreview } from '@/features/tailoring/components/TailoredDocumentsPreview';

import { DocumentEditor } from './DocumentEditor';
import { DocumentTabs } from './DocumentTabs';
import { EditorErrorBoundary } from './EditorErrorBoundary';
import { SaveIndicator } from './SaveIndicator';
import { useDocumentAutosave } from '../hooks/useDocumentAutosave';
import { useDocumentEditor } from '../hooks/useDocumentEditor';

import type { SaveState } from '../saveState';
import type { TailoredDocumentKind, TailoringRun } from '@/features/tailoring/types';

export interface DocumentWorkspaceProps {
  /** A `succeeded` run — both documents present (TR-5). The caller keys this component on `run.id`. */
  readonly run: TailoringRun;
}

/** The AC-36 sentence, byte for byte: where the CV now lives, said where it is now written. */
const STORAGE_NOTICE =
  'These documents are stored for 24 hours and are never kept in your browser.';

/**
 * The editor over one run — the **container** for both documents (technical plan, "The editor").
 *
 * It is two components. The outer one is the error boundary (AC-35): `useDocumentEditor` parses
 * the document through the bridge as it creates the instance, so a document the bridge cannot
 * parse throws *from a hook*, which is to say from the render of whichever component calls it —
 * and a boundary catches only what is thrown below it. Both hooks live in `DocumentEditors`, the
 * boundary wraps that, and the fallback is 1.3's read-only preview of **both** documents, because
 * that is the preview that exists and a person whose CV would not open still needs to read it.
 * One boundary, not one per pane: a fallback that shows both documents inside one pane would
 * show the other document twice.
 *
 * `key={run.id}` is on `DocumentEditors`, the component that owns the instances: a different run
 * is a different pair of editors, and nothing recreates an editor because a prop changed.
 */
export function DocumentWorkspace({ run }: DocumentWorkspaceProps): React.JSX.Element {
  return (
    <EditorErrorBoundary fallback={<EditorFallback run={run} />}>
      <DocumentEditors key={run.id} run={run} />
    </EditorErrorBoundary>
  );
}

/**
 * E-29: the read-only preview and the sentence. **No `PUT` can be issued from here** — there is no
 * editor, no autosave hook, no debounce; the fallback is the same component 1.3 shipped, holding
 * the same two strings the run already carries.
 */
function EditorFallback({ run }: { readonly run: TailoringRun }): React.JSX.Element {
  return (
    <div className="space-y-4">
      <p role="alert" className="text-sm text-slate-700">
        We couldn&apos;t open this document in the editor.
      </p>
      <TailoredDocumentsPreview
        tailoredCv={{
          text: run.tailored_cv ?? '',
          characterCount: run.tailored_cv_character_count,
        }}
        coverLetter={{
          text: run.cover_letter ?? '',
          characterCount: run.cover_letter_character_count,
        }}
      />
    </div>
  );
}

/** `/runs/:runId/:document` — `cv` unless the URL says the other one; `RunPage` validates E-28. */
function visibleDocumentOf(segment: string | undefined): TailoredDocumentKind {
  return segment === 'cover_letter' ? 'cover_letter' : 'cv';
}

/**
 * Both editors, both autosaves, the tabs, the visible document's indicator, and the footer.
 *
 * Both `useDocumentEditor`s and both `useDocumentAutosave`s are called at the top level,
 * unconditionally: hooks cannot be conditional, and a succeeded run has both documents, so there
 * is nothing to condition on. Both `DocumentEditor`s are rendered for the life of the page and the
 * URL's `:document` decides which is `hidden` (AC-30) — the inactive instance keeps its document,
 * its undo history and its unsaved edits, and switching tabs is a visibility change, not a mount.
 * The autosave is told which pane is visible, and flushes the one that just left (AC-31).
 *
 * `tailored_cv` and `cover_letter` are `null` while a run is in flight (the polling contract), and
 * the type says so. This component is only rendered on `succeeded`, where both are set; `?? ''`
 * states that in a way the compiler accepts without a `!`, and an empty editor is what a reader
 * would see if the claim were ever wrong.
 *
 * **Two `useEffect`s, both synchronizing with something outside React.** The first makes both
 * editors read-only once either save has answered 401 (AC-34): the session is the run's, not the
 * document's, so the letter goes read-only with the CV. It is an effect because the fact is
 * learned by the autosave hooks, which need the editor handles and therefore run *after* the
 * editor hooks — it cannot be a prop known in the same render — and because `setEditable` on an
 * instance is exactly the imperative call effects exist for. Nothing ever sets it back. The
 * second registers `beforeunload` while either document is `dirty` or `saving` (E-32): the
 * browser's own prompt is all a handler may do there, and it is registered only while it would
 * be true, so a person leaving a saved document is not nagged.
 *
 * After a 401 the footer's claim ("stored for 24 hours") is no longer true — the documents are
 * gone from the server — so the footer becomes the notice that says so, with the link home; the
 * text stays on screen above it for copying out, and nothing navigates by itself.
 */
function DocumentEditors({ run }: { readonly run: TailoringRun }): React.JSX.Element {
  const params = useParams<'document'>();
  const visible = visibleDocumentOf(params.document);

  const cv = useDocumentEditor(run.tailored_cv ?? '', { editable: true });
  const coverLetter = useDocumentEditor(run.cover_letter ?? '', { editable: true });
  const cvState = useDocumentAutosave(run.id, 'cv', cv, { visible: visible === 'cv' });
  const coverLetterState = useDocumentAutosave(run.id, 'cover_letter', coverLetter, {
    visible: visible === 'cover_letter',
  });

  const states: Readonly<Record<TailoredDocumentKind, SaveState>> = {
    cv: cvState,
    cover_letter: coverLetterState,
  };
  const expired = cvState.kind === 'expired' || coverLetterState.kind === 'expired';
  const leavingLosesWork = [cvState, coverLetterState].some(
    (state) => state.kind === 'dirty' || state.kind === 'saving',
  );

  useEffect(() => {
    if (!expired) {
      return;
    }
    for (const handle of [cv, coverLetter]) {
      if (handle.editor !== null && !handle.editor.isDestroyed) {
        handle.editor.setEditable(false);
      }
    }
  }, [expired, cv, coverLetter]);

  useEffect(() => {
    if (!leavingLosesWork) {
      return;
    }
    const warn = (event: BeforeUnloadEvent): void => {
      event.preventDefault();
    };
    window.addEventListener('beforeunload', warn);
    return () => {
      window.removeEventListener('beforeunload', warn);
    };
  }, [leavingLosesWork]);

  return (
    <div>
      <DocumentTabs runId={run.id} selected={visible} states={states} />
      <SaveIndicator state={states[visible]} />
      <DocumentEditor handle={cv} kind="cv" hidden={visible !== 'cv'} />
      <DocumentEditor
        handle={coverLetter}
        kind="cover_letter"
        hidden={visible !== 'cover_letter'}
      />
      <footer className="mt-4 text-sm text-slate-500">
        {expired ? (
          <p className="text-red-700">
            Your session has ended, and these documents were deleted after 24 hours. Nothing you
            type here can be saved now — the text stays on screen so you can copy it.{' '}
            <Link to="/" className="font-medium underline underline-offset-2">
              Back to the workspace
            </Link>
          </p>
        ) : (
          <p>{STORAGE_NOTICE}</p>
        )}
      </footer>
    </div>
  );
}
