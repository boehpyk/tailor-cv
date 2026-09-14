import { useState } from 'react';

import { useBaseCvs } from '@/features/intake/hooks/useBaseCvs';
import { latestBaseCv } from '@/features/intake/latestBaseCv';
import { useJobPostings } from '@/features/posting/hooks/useJobPostings';
import { latestPosting } from '@/features/posting/latestPosting';

import { TailorLaunch } from './TailorLaunch';
import { TailoredDocumentsPreview } from './TailoredDocumentsPreview';
import { TailoringFailureNotice } from './TailoringFailureNotice';
import { TailoringProgress } from './TailoringProgress';
import { TailoringRejectionNotice } from './TailoringRejectionNotice';
import { activeTailoringRunId, rejectionMessage } from '../apiErrorCopy';
import { useCreateTailoringRun } from '../hooks/useCreateTailoringRun';
import { useTailoringRun } from '../hooks/useTailoringRun';
import { useTailoringRuns } from '../hooks/useTailoringRuns';
import { latestTailoringRun } from '../latestTailoringRun';
import { checkBaseCv, checkJobPosting, launchInput } from '../launchReadiness';
import { viewOfWatchedRun } from '../runView';

import type { RunView } from '../runView';

/**
 * Whether the launch control belongs on screen alongside this view.
 *
 * Hidden while a run is loading or working — a second `POST` would only earn a 409 — and hidden
 * beside a failure that already offers **Try again**, so the user is not shown two buttons that do
 * the same thing. Shown next to a retryable failure only when Try again cannot work (an input went
 * missing since), because the launch's checklist is where that reason is written down.
 */
function offersLaunch(view: RunView, canStart: boolean): boolean {
  switch (view.kind) {
    case 'none':
    case 'succeeded':
      return true;
    case 'loading':
    case 'working':
      return false;
    case 'failed':
      return !view.retryable || !canStart;
    case 'unreadable':
      return !view.canCheckAgain;
  }
}

/**
 * The tailoring surface — a **container**. It owns the four states (loading, empty, working,
 * success) and the three error kinds (A: rejected before the request, B: rejected by the API, C: the
 * run failed); the markup for each lives in `TailorLaunch`, `TailoringProgress`,
 * `TailoredDocumentsPreview`, `TailoringRejectionNotice` and `TailoringFailureNotice`.
 *
 * **No props.** The base CV and the job posting are server state, so they are read from TanStack
 * Query where they are needed — the same query keys the sibling panels use, so one cache and one
 * request per list. And the CV and posting to tailor are picked with **the same `latestBaseCv` /
 * `latestPosting` functions** those panels use to decide what to show, so this panel cannot tailor
 * a different CV from the one on screen.
 *
 * **State.** Server state stays in TanStack Query and is never copied into `useState`. The one
 * piece of local state is `chosenRunId`, a run the user explicitly asked to watch — the one just
 * created, or the one a 409 named. When it is `null`, the watched run is **derived during render**
 * from the list's newest item, which is what makes a refresh reattach to a run in progress instead
 * of offering to pay for another. Nothing is written to `localStorage`: the server already remembers.
 *
 * **No `useEffect` here.** Polling is `useTailoringRun`'s `refetchInterval`; the elapsed clock is
 * `TailoringProgress`'s, and it is the only effect in the feature.
 */
export function TailorPanel(): React.JSX.Element {
  const baseCvs = useBaseCvs();
  const jobPostings = useJobPostings();
  const runs = useTailoringRuns();
  const create = useCreateTailoringRun();
  const [chosenRunId, setChosenRunId] = useState<string | null>(null);

  // Hooks cannot sit behind the early returns below, so the watched id is derived before them and
  // is simply `null` (a disabled query, no request) while the list is still loading.
  const latestRun = runs.data === undefined ? null : latestTailoringRun(runs.data.items);
  const watchedRunId = chosenRunId ?? latestRun?.id ?? null;
  const watched = useTailoringRun(watchedRunId);

  if (baseCvs.isError || jobPostings.isError || runs.isError) {
    // Not one of the three error kinds: this is the panel's own inputs failing to load, which says
    // nothing about any run. Reloading is free here — no run is started by loading the page.
    return (
      <p role="alert" className="text-sm text-red-700">
        We couldn&apos;t load what tailoring needs. Reload the page to try again.
      </p>
    );
  }

  // Loading: no button yet. A control that is about to be replaced reads as a flicker, and a
  // checklist drawn before the lists settle would tell a user with a CV to go and add one.
  if (baseCvs.isPending || jobPostings.isPending || runs.isPending) {
    return (
      <p role="status" className="text-sm text-slate-500">
        Loading…
      </p>
    );
  }

  const baseCv = checkBaseCv(latestBaseCv(baseCvs.data.items));
  const jobPosting = checkJobPosting(latestPosting(jobPostings.data.items));
  const input = launchInput(baseCv, jobPosting);
  const view = viewOfWatchedRun(watchedRunId, watched.data, watched.error, latestRun);

  function launch(): void {
    if (input === null) {
      // Error A. The control is already disabled; this guard makes "no request without both
      // inputs" true even for a caller that forgets that.
      return;
    }
    create.mutate(input, {
      onSuccess: (created) => {
        setChosenRunId(created.id);
      },
    });
  }

  const rejection = create.error;
  const activeRunId = rejection === null ? null : activeTailoringRunId(rejection);

  return (
    <div className="space-y-4">
      {view.kind === 'loading' && (
        <p role="status" className="text-sm text-slate-500">
          Loading your tailoring run…
        </p>
      )}

      {view.kind === 'working' && (
        <TailoringProgress key={`${view.runId}:${view.status}`} status={view.status} />
      )}

      {view.kind === 'succeeded' && (
        <TailoredDocumentsPreview tailoredCv={view.tailoredCv} coverLetter={view.coverLetter} />
      )}

      {view.kind === 'failed' && (
        <TailoringFailureNotice
          reason={view.reason}
          retryable={view.retryable}
          canStart={input !== null}
          isStarting={create.isPending}
          onRetry={launch}
        />
      )}

      {view.kind === 'unreadable' && (
        <div
          role="alert"
          className="space-y-2 rounded-md border border-slate-300 bg-slate-50 px-3 py-2"
        >
          <p className="text-sm font-medium text-slate-800">{view.message}</p>
          {view.canCheckAgain && (
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
          )}
        </div>
      )}

      {offersLaunch(view, input !== null) && (
        <TailorLaunch
          baseCv={baseCv}
          jobPosting={jobPosting}
          hasPreviousRun={latestRun !== null}
          isStarting={create.isPending}
          onLaunch={launch}
        />
      )}

      {rejection !== null && (
        <TailoringRejectionNotice
          message={rejectionMessage(rejection)}
          onViewActiveRun={
            activeRunId === null
              ? null
              : () => {
                  // Attach to the run the 409 named — not the list's newest, which may be an older
                  // run entirely — and clear the rejection, which has now been acted on.
                  setChosenRunId(activeRunId);
                  create.reset();
                }
          }
        />
      )}
    </div>
  );
}
