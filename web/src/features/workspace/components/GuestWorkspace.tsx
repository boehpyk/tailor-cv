import { GuestTailorOffer } from '@/features/billing/components/GuestTailorOffer';
import { BaseCvUploadPanel } from '@/features/intake/components/BaseCvUploadPanel';
import { useBaseCvs } from '@/features/intake/hooks/useBaseCvs';
import { latestBaseCv } from '@/features/intake/latestBaseCv';
import { JobPostingPanel } from '@/features/posting/components/JobPostingPanel';
import { useJobPostings } from '@/features/posting/hooks/useJobPostings';
import { latestPosting } from '@/features/posting/latestPosting';
import { useTailoringRuns } from '@/features/tailoring/hooks/useTailoringRuns';
import { latestTailoringRun } from '@/features/tailoring/latestTailoringRun';
import { FitScorePanel } from '@/features/scoring/components/FitScorePanel';
import { FIT_SCORE_HEADING } from '@/features/scoring/fitScoreCopy';
import { checkBaseCv, checkJobPosting, launchInput } from '@/features/tailoring/launchReadiness';

import { InputTabs } from './InputTabs';
import { LatestRunCard } from './LatestRunCard';
import { useWorkspaceTab } from '../hooks/useWorkspaceTab';
import { BASE_CV_INTRO, OPERATOR_ACCESS_NOTE } from '../workspaceCopy';

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

/** The sections' small-caps headings: slate-600, which holds 4.5:1 on every surface (AC-37). */
const EYEBROW_CLASS = 'text-xs font-semibold tracking-wide text-slate-600 uppercase';

/**
 * The four landmarks: each `<section>` keeps its heading (they are what `App.test.tsx` pairs up),
 * and the two input sections are the tab panels' content. The shell is drawn identically in every
 * state so the page's structure never jumps as data lands. The fit-score section (slice 5.1, OQ-10)
 * and then the tailoring section ride in `InputTabs`' panel column (web-design-refresh AC-14),
 * beside the rail, which keeps its three steps: a score is not a step.
 */
function Shell({
  tab,
  onTabChange,
  baseCvPanel,
  jobPostingPanel,
  fitScore,
  tailoring,
}: {
  readonly tab: WorkspaceTab | null;
  readonly onTabChange: (tab: WorkspaceTab) => void;
  readonly baseCvPanel: ReactNode;
  readonly jobPostingPanel: ReactNode;
  readonly fitScore: ReactNode;
  readonly tailoring: ReactNode;
}): React.JSX.Element {
  return (
    <InputTabs
      tab={tab}
      onTabChange={onTabChange}
      baseCvPanel={
        <section aria-labelledby="base-cv-heading" className="mb-10">
          <h2 id="base-cv-heading" className={EYEBROW_CLASS}>
            Your base CV
          </h2>
          <p className="mt-2 mb-6 font-display text-3xl font-bold tracking-tight text-slate-900 sm:text-4xl">
            {BASE_CV_INTRO}
          </p>
          {baseCvPanel}
        </section>
      }
      jobPostingPanel={
        <section aria-labelledby="job-posting-heading" className="mb-10">
          <h2 id="job-posting-heading" className={`mb-4 ${EYEBROW_CLASS}`}>
            The job you are applying for
          </h2>
          {jobPostingPanel}
        </section>
      }
    >
      <section aria-labelledby="fit-score-heading" className="mb-10">
        <h2 id="fit-score-heading" className={`mb-4 ${EYEBROW_CLASS}`}>
          {FIT_SCORE_HEADING}
        </h2>
        {fitScore}
        {/* Slice 5.2 (AC-36): a guest scores for free and is offered an account for the tailoring.
            Static, and a sibling of the panel, so the panel keeps its place as the data lands. */}
        <div className="mt-6">
          <GuestTailorOffer placement="workspace" />
        </div>
      </section>
      <section aria-labelledby="tailoring-heading" className="mb-10">
        <h2 id="tailoring-heading" className={`mb-4 ${EYEBROW_CLASS}`}>
          Your tailored CV and cover letter
        </h2>
        {tailoring}
      </section>
    </InputTabs>
  );
}

/**
 * The **guest** workspace — the `/` route's body for an anonymous visitor (slice 2.3, AC-38), moved
 * here unchanged from 1.4–2.2's `WorkspacePage`. What follows is that component's own account of
 * itself, still true.
 *
 * A **container**. It owns the three list queries (`useBaseCvs`, `useJobPostings`,
 * `useTailoringRuns`).
 *
 * **No launch since slice 5.2** (AC-15, AC-36): a guest scores for free and is offered an account
 * (`GuestTailorOffer`) for the tailoring. The latest-run card stays for a run made before release.
 *
 * **State.** Everything from the API stays in TanStack Query — the same query keys the panels use,
 * so one cache and one request per list. Which tab is open is the URL (`useWorkspaceTab`). There
 * is no `useState` here at all.
 *
 * **No progress stepper** (slice 5.2): it drew *Extracting CV → Fetching job → Tailoring* for a
 * run a guest can no longer start. The workspace does not poll a run either — the run page does,
 * and the latest-run card (*In progress → View*) is the way there.
 *
 * **Loading** keeps the three landmarks and shows no tabs (`tab={null}`): the derived default needs
 * the CV list, and a tab strip that switched its own selection when the data landed would read as
 * a flicker. **Error** is any list failing — a whole-workspace sentence, because the page cannot say
 * anything true about the inputs without them, and reloading is free (no run starts on load).
 */
export function GuestWorkspace(): React.JSX.Element {
  const baseCvs = useBaseCvs();
  const jobPostings = useJobPostings();
  const runs = useTailoringRuns();

  // Derived before the early returns so the hook order is fixed; `missing` while the list loads.
  const baseCv = checkBaseCv(baseCvs.data === undefined ? null : latestBaseCv(baseCvs.data.items));
  const { tab, setTab } = useWorkspaceTab(hasUsableBaseCv(baseCv));
  if (baseCvs.isError || jobPostings.isError || runs.isError) {
    return (
      <p role="alert" className="text-sm font-medium text-danger-600">
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
        // No pair while the lists load, so the panel reads nothing yet: it says what it needs.
        fitScore={<FitScorePanel pair={null} />}
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

  return (
    <Shell
      tab={tab}
      onTabChange={setTab}
      baseCvPanel={<BaseCvUploadPanel />}
      jobPostingPanel={<JobPostingPanel />}
      // The same pair a launch would send; the panel reads its stored score and never scores on
      // its own: only its button sends the `POST`.
      fitScore={<FitScorePanel pair={input} />}
      tailoring={
        // No progress stepper since slice 5.2: a guest starts no run, so its *Tailoring* stage
        // would only advertise what a guest cannot do. The latest-run card covers an older run.
        <div className="space-y-6 rounded-2xl border border-slate-200 bg-white p-5 sm:p-7">
          {/* Was TailorLaunch's line (4.x AC-37): a guest's data is still stored, so it is still said. */}
          <p className="text-sm text-slate-600">{OPERATOR_ACCESS_NOTE}</p>

          {/* A run made before slice 5.2 stays readable and editable (AC-17). */}
          {latestRun !== null && <LatestRunCard run={latestRun} />}
        </div>
      }
    />
  );
}
