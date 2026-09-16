import { useIsMutating } from '@tanstack/react-query';
import { useNavigate } from 'react-router';

import { BaseCvUploadPanel } from '@/features/intake/components/BaseCvUploadPanel';
import { useBaseCvs } from '@/features/intake/hooks/useBaseCvs';
import { uploadBaseCvMutationKey } from '@/features/intake/hooks/useUploadBaseCv';
import { latestBaseCv } from '@/features/intake/latestBaseCv';
import { JobPostingPanel } from '@/features/posting/components/JobPostingPanel';
import { createJobPostingMutationKey } from '@/features/posting/hooks/useCreateJobPosting';
import { useJobPostings } from '@/features/posting/hooks/useJobPostings';
import { latestPosting } from '@/features/posting/latestPosting';
import { activeTailoringRunId, rejectionMessage } from '@/features/tailoring/apiErrorCopy';
import { TailoringRejectionNotice } from '@/features/tailoring/components/TailoringRejectionNotice';
import { TailorLaunch } from '@/features/tailoring/components/TailorLaunch';
import { useCreateTailoringRun } from '@/features/tailoring/hooks/useCreateTailoringRun';
import { useTailoringRuns } from '@/features/tailoring/hooks/useTailoringRuns';
import { latestTailoringRun } from '@/features/tailoring/latestTailoringRun';
import { checkBaseCv, checkJobPosting, launchInput } from '@/features/tailoring/launchReadiness';
import { isActiveTailoringRunStatus } from '@/features/tailoring/types';

import { InputTabs } from './InputTabs';
import { LatestRunCard } from './LatestRunCard';
import { ProgressStepper } from './ProgressStepper';
import { useWorkspaceTab } from '../hooks/useWorkspaceTab';
import { deriveProgress } from '../progress';

import type { WorkspaceTab } from '../hooks/useWorkspaceTab';
import type { BaseCvCheck } from '@/features/tailoring/launchReadiness';
import type { ReactNode } from 'react';

/**
 * The derived tab default's one input (AC-23): does the session already have a CV the user need
 * not act on? A CV that is `extracted` or still being read counts; one that failed extraction does
 * not, because the next thing to do with it is upload another — which is on the *Base CV* tab. UX
 * only: nothing here decides whether a run may start.
 */
function hasUsableBaseCv(baseCv: BaseCvCheck): boolean {
  return baseCv.state === 'ready' || baseCv.state === 'reading';
}

/**
 * The three landmarks, unchanged since 1.3's `App`: each `<section>` keeps its heading (they are
 * what `App.test.tsx` pairs up), and the two input sections are the tab panels' content. The shell
 * is drawn identically in every state so the page's structure never jumps as data lands.
 */
function Shell({
  tab,
  onTabChange,
  baseCvPanel,
  jobPostingPanel,
  tailoring,
}: {
  readonly tab: WorkspaceTab | null;
  readonly onTabChange: (tab: WorkspaceTab) => void;
  readonly baseCvPanel: ReactNode;
  readonly jobPostingPanel: ReactNode;
  readonly tailoring: ReactNode;
}): React.JSX.Element {
  return (
    <>
      <InputTabs
        tab={tab}
        onTabChange={onTabChange}
        baseCvPanel={
          <section aria-labelledby="base-cv-heading" className="mb-10">
            <h2 id="base-cv-heading" className="mb-3 text-sm font-medium text-slate-500 uppercase">
              Your base CV
            </h2>
            {baseCvPanel}
          </section>
        }
        jobPostingPanel={
          <section aria-labelledby="job-posting-heading" className="mb-10">
            <h2
              id="job-posting-heading"
              className="mb-3 text-sm font-medium text-slate-500 uppercase"
            >
              The job you are applying for
            </h2>
            {jobPostingPanel}
          </section>
        }
      />

      <section aria-labelledby="tailoring-heading" className="mb-10">
        <h2 id="tailoring-heading" className="mb-3 text-sm font-medium text-slate-500 uppercase">
          Your tailored CV and cover letter
        </h2>
        {tailoring}
      </section>
    </>
  );
}

/**
 * The `/` route — a **container**. It owns the three list queries (`useBaseCvs`, `useJobPostings`,
 * `useTailoringRuns`), reads the two panel mutations' in-flight state from the mutation cache,
 * owns the launch mutation, and navigates to `/runs/{id}` when a run is created (AC-25).
 *
 * **State.** Everything from the API stays in TanStack Query — the same query keys the panels use,
 * so one cache and one request per list. Which tab is open is the URL (`useWorkspaceTab`). There
 * is no `useState` here at all: the one piece of local state 1.3's `TailorPanel` kept, the
 * "chosen" run id, is now the route — a created run is *navigated to*, and a 409's active run is
 * navigated to as well. Nothing to remember means nothing to get out of sync.
 *
 * **The panels' pending flags** come from `useIsMutating({ mutationKey })`: each panel owns its
 * mutation and keeps its internals, and the workspace observes the shared mutation cache instead
 * of threading an `onPendingChange` callback down and a boolean back up. The flag is a fact about
 * a request on the wire, which the cache already knows.
 *
 * **Stage 3 is not live here.** The stepper's `run` is `none`: the workspace does not poll the
 * run — the run page does, and the latest-run card is the way there. A stage-3 body drawn from the
 * list's snapshot would show a clock that never ticks over a status that never changes, which is
 * worse than a card that says *In progress → View*.
 *
 * **Loading** keeps the three landmarks and shows no tabs (`tab={null}`): the derived default needs
 * the CV list, and a tab strip that switched its own selection when the data landed would read as
 * a flicker. **Error** is any list failing — a whole-workspace sentence, because the page cannot say
 * anything true about the inputs without them, and reloading is free (no run starts on load).
 */
export function WorkspacePage(): React.JSX.Element {
  const baseCvs = useBaseCvs();
  const jobPostings = useJobPostings();
  const runs = useTailoringRuns();
  const isUploadingBaseCv = useIsMutating({ mutationKey: uploadBaseCvMutationKey }) > 0;
  const isSubmittingJobPosting = useIsMutating({ mutationKey: createJobPostingMutationKey }) > 0;
  const create = useCreateTailoringRun();
  const navigate = useNavigate();

  // Derived before the early returns so the hook order is fixed; `missing` while the list loads.
  const baseCv = checkBaseCv(baseCvs.data === undefined ? null : latestBaseCv(baseCvs.data.items));
  const { tab, setTab } = useWorkspaceTab(hasUsableBaseCv(baseCv));

  if (baseCvs.isError || jobPostings.isError || runs.isError) {
    return (
      <p role="alert" className="text-sm text-red-700">
        We couldn&apos;t load your workspace. Reload the page to try again.
      </p>
    );
  }

  if (baseCvs.isPending || jobPostings.isPending || runs.isPending) {
    return (
      <Shell
        tab={null}
        onTabChange={setTab}
        baseCvPanel={<BaseCvUploadPanel />}
        jobPostingPanel={<JobPostingPanel />}
        tailoring={
          <p role="status" className="text-sm text-slate-500">
            Loading…
          </p>
        }
      />
    );
  }

  const jobPosting = checkJobPosting(latestPosting(jobPostings.data.items));
  const input = launchInput(baseCv, jobPosting);
  const latestRun = latestTailoringRun(runs.data.items);
  const runInProgress = latestRun !== null && isActiveTailoringRunStatus(latestRun.status);
  const progress = deriveProgress({
    baseCv,
    isUploadingBaseCv,
    jobPosting,
    isSubmittingJobPosting,
    run: { kind: 'none' },
  });

  function launch(): void {
    if (input === null) {
      // Error A. The control is already disabled; this guard makes "no request without both
      // inputs" true even for a caller that forgets that.
      return;
    }
    create.mutate(input, {
      onSuccess: (created) => {
        // The route table sends `/runs/{id}` on to `/runs/{id}/cv`; deciding the default document
        // is its job, not this page's.
        void navigate(`/runs/${created.id}`);
      },
    });
  }

  const rejection = create.error;
  const activeRunId = rejection === null ? null : activeTailoringRunId(rejection);

  return (
    <Shell
      tab={tab}
      onTabChange={setTab}
      baseCvPanel={<BaseCvUploadPanel />}
      jobPostingPanel={<JobPostingPanel />}
      tailoring={
        <div className="space-y-4">
          <ProgressStepper
            progress={progress}
            run={{ kind: 'none' }}
            canStart={input !== null}
            isStarting={create.isPending}
            onRetry={launch}
          />

          <LatestRunCard run={latestRun} />

          <TailorLaunch
            baseCv={baseCv}
            jobPosting={jobPosting}
            hasPreviousRun={latestRun !== null}
            isStarting={create.isPending}
            runInProgress={runInProgress}
            onLaunch={launch}
          />

          {rejection !== null && (
            <TailoringRejectionNotice
              message={rejectionMessage(rejection)}
              onViewActiveRun={
                activeRunId === null
                  ? null
                  : () => {
                      // Go to the run the 409 named — not the list's newest, which may be an
                      // older run entirely.
                      void navigate(`/runs/${activeRunId}`);
                    }
              }
            />
          )}
        </div>
      }
    />
  );
}
