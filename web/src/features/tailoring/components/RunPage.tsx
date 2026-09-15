import { Link, useNavigate, useParams } from 'react-router';

import { DocumentWorkspace } from '@/features/editor/components/DocumentWorkspace';
import { ProgressStepper } from '@/features/workspace/components/ProgressStepper';
import { progressOfRun } from '@/features/workspace/progress';

import { NotFoundPage } from './NotFoundPage';
import { TailoringRejectionNotice } from './TailoringRejectionNotice';
import { activeTailoringRunId, rejectionMessage } from '../apiErrorCopy';
import { useCreateTailoringRun } from '../hooks/useCreateTailoringRun';
import { useTailoringRun } from '../hooks/useTailoringRun';
import { viewOfWatchedRun } from '../runView';

import type { TailoringRun } from '../types';

/** The two documents a run page can show, as they are spelt in the URL (`/runs/:runId/:document`). */
type DocumentSegment = 'cv' | 'cover_letter';

function parseDocument(value: string | undefined): DocumentSegment | null {
  return value === 'cv' || value === 'cover_letter' ? value : null;
}

const HOME_LINK_CLASS = 'font-medium text-slate-900 underline underline-offset-2';

function BackToWorkspace(): React.JSX.Element {
  return (
    <p className="text-sm">
      <Link to="/" className={HOME_LINK_CLASS}>
        Back to the workspace
      </Link>
    </p>
  );
}

/**
 * The `/runs/:runId/:document` route — a **container** (AC-26).
 *
 * It reads `:runId` and `:document` from the URL, watches the run with `useTailoringRun` (1.3's
 * poller, unchanged: the `refetchInterval` is the loop, and it stops on a terminal status or a
 * 4xx), derives the view with `viewOfWatchedRun` and renders the stepper with stage 3 live.
 *
 * **No `POST` on load.** The run exists because the URL says so; the page only ever reads it. That
 * is what makes a refresh — or a link opened tomorrow — reattach to a run in progress instead of
 * paying for another (AC-25).
 *
 * **Four states, three error kinds.** Loading; working (the stepper, 1.3's two sentences, no retry
 * control); failed (1.3's notice, **Try again** only when the API said `retryable`); succeeded (the
 * editor). The errors: a 404 and a 401 each get their own sentence and a link home, because the
 * run is gone or the session is and no re-read will change that; a network failure or a 5xx gets
 * 1.3's *unreadable* state with **Check again**, because the run may well still be working, and a
 * user who reads "failed" there refreshes and pays twice.
 *
 * **Try again creates a new run** from the ids this run carries — `base_cv_id` and
 * `job_posting_id` are on the wire type — and navigates to it. The page does not read the CV and
 * posting lists to decide whether it *may* retry: the API re-checks both on every `POST` (a 404 for
 * an input that has since expired, a 409 for one that is not extracted) and its answer is rendered
 * as error B, the same as on the workspace.
 *
 * `:document` is validated here (E-28: `/runs/{id}/banana` is a not-found page, not a blank one)
 * and read again inside `DocumentWorkspace`, where it chooses which of the two editors is visible
 * (AC-30). A succeeded run renders the workspace, which is handed the **run**, not the view: the
 * editors seed from the wire documents and the autosave writes the run's query key, so the
 * workspace's unit is the resource. `viewOfWatchedRun` still decides *that* the run succeeded,
 * and the stepper above still shows stage 3 done. The workspace keys its editors on the run id,
 * and its error boundary (AC-35) is what turns a document the bridge cannot parse into 1.3's
 * read-only preview rather than a blank page.
 */
export function RunPage(): React.JSX.Element {
  const params = useParams<'runId' | 'document'>();
  const runId = params.runId ?? null;
  const documentSegment = parseDocument(params.document);
  const navigate = useNavigate();
  const watched = useTailoringRun(runId);
  const create = useCreateTailoringRun();

  if (runId === null || documentSegment === null) {
    return <NotFoundPage />;
  }

  const view = viewOfWatchedRun(runId, watched.data, watched.error, null);

  function retry(run: TailoringRun): void {
    create.mutate(
      { base_cv_id: run.base_cv_id, job_posting_id: run.job_posting_id },
      {
        onSuccess: (created) => {
          void navigate(`/runs/${created.id}`);
        },
      },
    );
  }

  const rejection = create.error;
  const activeRunId = rejection === null ? null : activeTailoringRunId(rejection);

  return (
    <section aria-labelledby="run-heading" className="mb-10 space-y-4">
      <h2 id="run-heading" className="mb-3 text-sm font-medium text-slate-500 uppercase">
        Your tailored CV and cover letter
      </h2>

      {view.kind === 'loading' && (
        <p role="status" className="text-sm text-slate-500">
          Loading your tailoring run…
        </p>
      )}

      {view.kind === 'unreadable' && (
        <div
          role="alert"
          className="space-y-2 rounded-md border border-slate-300 bg-slate-50 px-3 py-2"
        >
          <p className="text-sm font-medium text-slate-800">{view.message}</p>
          {view.canCheckAgain ? (
            <button
              type="button"
              onClick={() => {
                void watched.refetch();
              }}
              disabled={watched.isFetching}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 disabled:opacity-60"
            >
              {watched.isFetching ? 'Checking…' : 'Check again'}
            </button>
          ) : (
            <BackToWorkspace />
          )}
        </div>
      )}

      {(view.kind === 'working' || view.kind === 'failed' || view.kind === 'succeeded') && (
        <ProgressStepper
          progress={progressOfRun(view)}
          run={view}
          canStart={true}
          isStarting={create.isPending}
          onRetry={() => {
            // `failed` implies the detail query answered, so `data` is set; the narrowing is for
            // the type, which cannot see that, and the no-op branch is unreachable in practice.
            if (watched.data !== undefined) {
              retry(watched.data);
            }
          }}
        />
      )}

      {/* `succeeded` implies the detail query answered (a list summary cannot show the documents,
          so the view never says `succeeded` from one), hence `data` is set; the narrowing is for
          the type, the same as `onRetry` above. */}
      {view.kind === 'succeeded' && watched.data !== undefined && (
        <DocumentWorkspace run={watched.data} />
      )}

      {rejection !== null && (
        <TailoringRejectionNotice
          message={rejectionMessage(rejection)}
          onViewActiveRun={
            activeRunId === null
              ? null
              : () => {
                  void navigate(`/runs/${activeRunId}`);
                }
          }
        />
      )}

      {(view.kind === 'failed' || view.kind === 'succeeded') && <BackToWorkspace />}
    </section>
  );
}
