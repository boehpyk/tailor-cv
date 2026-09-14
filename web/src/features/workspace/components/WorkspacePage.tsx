import { BaseCvUploadPanel } from '@/features/intake/components/BaseCvUploadPanel';
import { JobPostingPanel } from '@/features/posting/components/JobPostingPanel';
import { TailorLaunch } from '@/features/tailoring/components/TailorLaunch';

import { InputTabs } from './InputTabs';
import { LatestRunCard } from './LatestRunCard';
import { ProgressStepper } from './ProgressStepper';
import { useWorkspaceTab } from '../hooks/useWorkspaceTab';

import type { ProgressView } from '../progress';

/** Nothing has happened yet. The only view the skeleton knows; `deriveProgress` replaces it in F7. */
const NOTHING_YET: ProgressView = {
  extracting: 'pending',
  fetching: 'pending',
  tailoring: 'pending',
};

/**
 * The `/` route — a **container**, once F7 gives it its queries. It will own the two list queries
 * and the run list, the two panel mutations' pending flags, the launch mutation and the navigation
 * to `/runs/{id}` on success (AC-25).
 *
 * F5 skeleton: the composition and the landmarks only. Each `<section>` keeps the heading it had in
 * 1.3's `App` (they are what `App.test.tsx` pairs up), the two real panels are mounted inside
 * the tabs so the panels exist, and the launch, stepper and card are handed inert values. No query
 * of its own, no derived state, no navigation.
 */
export function WorkspacePage(): React.JSX.Element {
  const { tab, setTab } = useWorkspaceTab(false);

  return (
    <>
      <InputTabs
        tab={tab}
        onTabChange={setTab}
        baseCvPanel={
          <section aria-labelledby="base-cv-heading" className="mb-10">
            <h2 id="base-cv-heading" className="mb-3 text-sm font-medium text-slate-500 uppercase">
              Your base CV
            </h2>
            <BaseCvUploadPanel />
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
            <JobPostingPanel />
          </section>
        }
      />

      <section aria-labelledby="tailoring-heading" className="mb-10">
        <h2 id="tailoring-heading" className="mb-3 text-sm font-medium text-slate-500 uppercase">
          Your tailored CV and cover letter
        </h2>
        <ProgressStepper
          progress={NOTHING_YET}
          run={{ kind: 'none' }}
          canStart={false}
          isStarting={false}
          onRetry={() => undefined}
        />
        <TailorLaunch
          baseCv={{ state: 'missing' }}
          jobPosting={{ state: 'missing' }}
          hasPreviousRun={false}
          isStarting={false}
          onLaunch={() => undefined}
        />
        <LatestRunCard run={null} />
      </section>
    </>
  );
}
