import { useCallback, useEffect } from 'react';
import { Link, useBlocker, useParams } from 'react-router';

import { TailoredDocumentsPreview } from '@/features/tailoring/components/TailoredDocumentsPreview';

import { DocumentEditor } from './DocumentEditor';
import { DocumentTabs } from './DocumentTabs';
import { EditorErrorBoundary } from './EditorErrorBoundary';
import { SaveIndicator } from './SaveIndicator';
import { useDocumentAutosave } from '../hooks/useDocumentAutosave';
import { useDocumentEditor } from '../hooks/useDocumentEditor';

import type { SaveState } from '../saveState';
import type { TailoredDocumentKind, TailoringRun } from '@/features/tailoring/types';
import type { BlockerFunction } from 'react-router';

/**
 * What a caller may render above the document tabs, as a function of the visible document's save
 * state. Named because two components declare it and the name says what the argument is for.
 */
export type RenderAboveTabs = (saveState: SaveState) => React.ReactNode;

export interface DocumentWorkspaceProps {
  /** A `succeeded` run — both documents present (TR-5). The caller keys this component on `run.id`. */
  readonly run: TailoringRun;
  /**
   * Rendered directly above the document tabs, given the **visible** document's `SaveState`
   * (AC-36, AC-40). `RunPage` passes the export bar; nothing else uses it, and it is optional so
   * that every 1.4 test which mounts this component unchanged still mounts it unchanged.
   *
   * **A render prop rather than an `onSaveStateChange` callback, and the reason is the whole
   * point of this seam.** The save state is computed by 1.4's machine inside `useDocumentAutosave`,
   * during render, from a reducer this component does not own. A callback could only hand it
   * upwards *after* that render, so the parent would need a `useState` to hold it and a
   * `useEffect` to push it there — a second copy of the state, one render behind the first, which
   * is the exact pattern the React conventions in CLAUDE.md forbid ("`useEffect` is for
   * synchronizing with something outside React — not deriving"). It would also re-render the whole
   * run page on every keystroke's `dirty`, and it would make the gate lie for one frame at the
   * worst possible moment: the frame in which the user just typed. The render prop moves the
   * *consumer* to where the state already is, so the state moves nowhere at all.
   */
  readonly renderAbove?: RenderAboveTabs;
}

/** The AC-36 sentence, byte for byte: where the CV now lives, said where it is now written. */
const STORAGE_NOTICE =
  'These documents are stored for 24 hours and are never kept in your browser.';

/**
 * The question asked before an in-app navigation would destroy an editor that holds text the
 * server has not confirmed (AC-34: "the text is never lost"). Shown through `window.confirm`, so
 * it has to read as a yes/no: OK leaves, Cancel stays. It says "may not be saved" and not "will be
 * lost", because that is what is true across the states that hold the lock: in `dirty` the
 * unmount hands the text to the mutation cache, which outlives this component, and in `saving`
 * the `PUT` on the wire lands whether or not the page is still here — while in `failed`,
 * `invalid`, `paused` and `conflict` nothing is on its way at all. The person cannot tell which
 * from the door, so the sentence promises neither. The browser's own `beforeunload` prompt cannot
 * show custom text, so this sentence is only ever seen on the router's door — but it is the same
 * lock (see `holdsUnsavedText`), and it is exported so a test asserts the words.
 */
export const UNSAVED_LEAVE_PROMPT =
  'Your latest edits may not be saved yet. Leave this page anyway?';

/**
 * Whether leaving now would lose text the server does not have — the one predicate behind both
 * exit doors (`beforeunload` and the router's `useBlocker`).
 *
 * It is not `dirty || saving`. In `failed`, `invalid`, `paused` and `conflict` the editor holds
 * text that never reached the server either: a save that failed three times, a document the
 * server refused, a save waiting out a 429, and a document the person has not yet chosen between.
 * Only two kinds are safe to leave: `saved` (the server has this text) and `expired` (the server
 * has deleted the run and nothing can be saved; the footer says so and offers the link home, and
 * blocking that link would trap the person on a page that can do nothing for them). The `switch`
 * is exhaustive with no `default` on purpose: a new `SaveState` kind must be placed on one side or
 * the other here before this compiles, rather than silently join the side that loses work.
 */
function holdsUnsavedText(state: SaveState): boolean {
  switch (state.kind) {
    case 'saved':
    case 'expired':
      return false;
    case 'dirty':
    case 'saving':
    case 'failed':
    case 'invalid':
    case 'paused':
    case 'conflict':
      return true;
  }
}

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
 *
 * `renderAbove` goes **inside** the boundary, because what it renders is a function of the save
 * state and the save state only exists where the autosave hooks are. So a document the bridge
 * cannot parse takes the export bar down with the editor: E-29's fallback is read-only, its save
 * state is not "saved" but *absent*, and a bar rendered there would have to invent one — the one
 * input AC-40's gate exists to respect. The fallback keeps its own job, which is to put the text
 * on screen so it can be copied out.
 */
export function DocumentWorkspace({ run, renderAbove }: DocumentWorkspaceProps): React.JSX.Element {
  return (
    <EditorErrorBoundary fallback={<EditorFallback run={run} />}>
      <DocumentEditors key={run.id} run={run} renderAbove={renderAbove} />
    </EditorErrorBoundary>
  );
}

/**
 * E-29: the read-only preview and the sentence. **No `PUT` can be issued from here** — there is no
 * editor, no autosave hook, no debounce; the fallback is the same component 1.3 shipped, holding
 * the same two strings the run already carries.
 *
 * The sentence is the preview's `closing`, so it is on the page **once**: the preview's own 1.3
 * footer ("editing comes next") would be untrue beside an editor that just failed, and a second
 * copy of E-29's sentence above the panes would be two matches for one string. `alert`, because
 * it is the answer to "where is the editor?" and a screen reader should hear it before the panes.
 */
function EditorFallback({ run }: { readonly run: TailoringRun }): React.JSX.Element {
  return (
    <TailoredDocumentsPreview
      tailoredCv={{
        text: run.tailored_cv ?? '',
        characterCount: run.tailored_cv_character_count,
      }}
      coverLetter={{
        text: run.cover_letter ?? '',
        characterCount: run.cover_letter_character_count,
      }}
      closing={
        <p role="alert" className="text-slate-700">
          We couldn&apos;t open this document in the editor.
        </p>
      }
    />
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
 * **Three `useEffect`s, each synchronizing with something outside React.** The first makes both
 * editors read-only once either save has answered 401 (AC-34): the session is the run's, not the
 * document's, so the letter goes read-only with the CV. It is an effect because the fact is
 * learned by the autosave hooks, which need the editor handles and therefore run *after* the
 * editor hooks — it cannot be a prop known in the same render — and because `setEditable` on an
 * instance is exactly the imperative call effects exist for. Nothing ever sets it back.
 *
 * The other two are **two exit doors, one lock** (E-32, AC-34). The lock is `holdsUnsavedText`
 * over both documents. The first door is the browser's: the second effect registers
 * `beforeunload` while the lock is held, and the browser's own prompt is all a handler may do
 * there. The second door is the router's, which `beforeunload` never sees: a `<Link>` — the run
 * page's *Back to the workspace*, the header's logo, any in-app navigation — unmounts this
 * component and destroys both editors without the browser noticing a thing. `useBlocker` holds
 * that door while the lock is held, and the third effect answers a blocked navigation with a
 * `window.confirm` of `UNSAVED_LEAVE_PROMPT`: OK proceeds to where the person was going, Cancel
 * resets the blocker and leaves them here, and both happen inside the effect that saw the block,
 * so the blocker never sits in `blocked` between renders. The tab switch is the one in-app
 * navigation that must **not** ask: it is a `<Link>` to this same run's other document, which is
 * a visibility change (AC-30) and a flush (AC-31), not a loss — so the blocker's function exempts
 * this run's two document URLs and blocks everything else. Both doors register only while the
 * lock is held, so a person leaving a saved document is not nagged at either — and neither is a
 * person leaving after a 401, whichever document answered it (see `leavingLosesWork`).
 *
 * Measured against `react-router` 7.18.3: a data router consults **one** blocker (the last one
 * registered) and warns about a second, which is why the lock lives here, over both documents,
 * and not once per autosave hook. `useBlocker` needs a data router (`createBrowserRouter`,
 * `createMemoryRouter`), which is what `router.tsx` and every test that mounts this component use.
 *
 * After a 401 the footer's claim ("stored for 24 hours") is no longer true — the documents are
 * gone from the server — so the footer becomes the notice that says so, with the link home; the
 * text stays on screen above it for copying out, and nothing navigates by itself.
 */
function DocumentEditors({
  run,
  renderAbove,
}: {
  readonly run: TailoringRun;
  // `| undefined` explicitly, because `exactOptionalPropertyTypes` distinguishes "absent" from
  // "present and undefined", and this is the latter: `DocumentWorkspace` always passes the prop on,
  // whether or not its own caller supplied one.
  readonly renderAbove: RenderAboveTabs | undefined;
}): React.JSX.Element {
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
  // A 401 is the session's, not one document's: once either save has answered it, both editors
  // are read-only and the server can take nothing more from this page — so the other document's
  // `failed` or `dirty` is not work that leaving would lose, and holding the doors would only ask
  // a person to stay for a save that can no longer happen.
  const leavingLosesWork =
    !expired && (holdsUnsavedText(cvState) || holdsUnsavedText(coverLetterState));

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

  // The browser's door.
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

  // The router's door. The function form rather than the boolean, because the tab switch is a
  // navigation to this same run's other document (`DocumentTabs` builds exactly these paths) and
  // must pass without a question; `pathname` alone is compared, so a query or hash on the same
  // document is a tab switch too. Anything else — another run, the workspace, a not-found URL —
  // unmounts this component and is held while the lock is.
  const runId = run.id;
  const shouldBlock = useCallback<BlockerFunction>(
    ({ nextLocation }) => {
      if (!leavingLosesWork) {
        return false;
      }
      const base = `/runs/${encodeURIComponent(runId)}/`;
      return (
        nextLocation.pathname !== `${base}cv` && nextLocation.pathname !== `${base}cover_letter`
      );
    },
    [leavingLosesWork, runId],
  );
  const blocker = useBlocker(shouldBlock);

  // Answer a blocked navigation. `window.confirm` is modal and synchronous, so by the time this
  // effect returns the blocker has either proceeded or been reset — it never stays `blocked`
  // across a render, which is what makes a "reset it when the lock releases" effect (the one
  // `react-router`'s own `usePrompt` carries) unnecessary here, and in fact unsafe beside a
  // deferred `proceed`: `updateBlocker` throws on `unblocked → proceeding`. `proceed` is called
  // synchronously for the same reason. The POP case (the back button) is safe without
  // `usePrompt`'s `setTimeout`: the router reverts the history entry first and `proceed` only
  // re-applies it after that revert has landed (`nextHistoryUpdatePromise`, measured in 7.18.3).
  useEffect(() => {
    if (blocker.state !== 'blocked') {
      return;
    }
    if (window.confirm(UNSAVED_LEAVE_PROMPT)) {
      blocker.proceed();
    } else {
      blocker.reset();
    }
  }, [blocker]);

  return (
    <div>
      {/* The visible document's state, read out of the same record the tabs and the indicator
          read. It is passed, never lifted: this component still owns nothing it did not own
          before, and the caller holds no copy of it. */}
      {renderAbove?.(states[visible])}
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
