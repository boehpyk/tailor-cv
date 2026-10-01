import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useNavigate } from 'react-router';

import { GuestWorkOffer } from '@/features/claim/components/GuestWorkOffer';
import { JobPostingPanel } from '@/features/posting/components/JobPostingPanel';
import { useJobPostings } from '@/features/posting/hooks/useJobPostings';
import { latestPosting } from '@/features/posting/latestPosting';
import { SavedBaseCvPicker } from '@/features/savedCvs/components/SavedBaseCvPicker';
import { useSavedBaseCvs } from '@/features/savedCvs/hooks/useSavedBaseCvs';
import { effectiveSelection } from '@/features/savedCvs/selection';
import { runLink } from '@/features/scope/scopeMap';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';
import { useLatestAccountRun } from '@/features/history/hooks/useLatestAccountRun';
import { activeTailoringRunId, rejectionMessage } from '@/features/tailoring/apiErrorCopy';
import { TailoringRejectionNotice } from '@/features/tailoring/components/TailoringRejectionNotice';
import { TailorLaunch } from '@/features/tailoring/components/TailorLaunch';
import {
  createTailoringRunMutationKey,
  useCreateTailoringRun,
} from '@/features/tailoring/hooks/useCreateTailoringRun';
import { checkBaseCv, checkJobPosting, launchInput } from '@/features/tailoring/launchReadiness';
import { isActiveTailoringRunStatus } from '@/features/tailoring/types';

import { LatestRunCard } from './LatestRunCard';
import { ACCOUNT_PROMISE, LATEST_RUN_ERROR_NOTE, RETRY_LABEL } from '../workspaceCopy';

import type { ReactNode } from 'react';

export interface AccountWorkspaceProps {
  /** The signed-in user — handed down by `AccountScope`, and the root of every account key. */
  readonly userId: string;
}

const HEADING_CLASS = 'mb-3 text-sm font-medium text-slate-500 uppercase';

function Section({
  id,
  title,
  children,
}: {
  readonly id: string;
  readonly title: string;
  readonly children: ReactNode;
}): React.JSX.Element {
  return (
    <section aria-labelledby={id} className="mb-10">
      <h2 id={id} className={HEADING_CLASS}>
        {title}
      </h2>
      {children}
    </section>
  );
}

/**
 * The **account** workspace — `/` for a signed-in user (AC-39…AC-42, AC-49), rendered inside
 * `AccountScope`, so every scoped hook below talks to `/api/me/` with the bearer and keys its cache
 * under `['auth', 'account', userId]`. A **container**.
 *
 * - **The promise first** (AC-49): what is kept, and what the AI provider sees — before the first
 *   launch, not after it.
 * - **Base CV** — the saved-CV picker in `mode: 'select'`. The user's choice is form state
 *   (`useState`, an id), and the CV a run uses is `effectiveSelection(list, choice)` — the same rule
 *   the picker draws with, so the checked radio and the launched CV cannot differ. The saved CV is
 *   referenced **directly**: nothing is copied (AC-39; the copy route itself is gone since 2.4).
 * - **Posting** — 1.2's panel, unchanged, in the account scope: `/api/me/job-postings?limit=1`
 *   and its create with the bearer (AC-40). Its card reads *Saved with your history*.
 * - **Launch** — `POST /api/me/tailoring-runs`, guarded against a same-tick double click by the
 *   mutation cache (H-58), then `/history/{id}` (AC-41). The latest-run card reads
 *   `useLatestAccountRun`; a 409 `tailoring_already_running` links to the active run.
 * - **Guest work** in this browser is offered, not hidden (`GuestWorkOffer`, slice 2.4's AC-37,
 *   amending 2.3's AC-42 notice): *Keep them in my account* moves it here, and the claim's own
 *   invalidations re-read the picker, the posting and the latest run.
 *
 * Every list here is server state in TanStack Query — the picker and this component share
 * `useSavedBaseCvs`, and the panel and this component share `useJobPostings`: one cache, one
 * request each. No 1.4 tab strip: the guest's tabs exist to steer a first upload, and here the
 * picker and the posting sit together.
 */
export function AccountWorkspace({ userId }: AccountWorkspaceProps): React.JSX.Element {
  const savedCvs = useSavedBaseCvs();
  const postings = useJobPostings();
  const latest = useLatestAccountRun(userId);
  const create = useCreateTailoringRun();
  const map = useScopeMap();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [chosenId, setChosenId] = useState<string | null>(null);

  const cvItems = savedCvs.data?.items ?? [];
  const selectedId = effectiveSelection(cvItems, chosenId);
  const baseCv = checkBaseCv(cvItems.find((cv) => cv.id === selectedId) ?? null);
  const jobPosting = checkJobPosting(
    postings.data === undefined ? null : latestPosting(postings.data.items),
  );
  const input = launchInput(baseCv, jobPosting);
  const latestRun = latest.data ?? null;
  const runInProgress = latestRun !== null && isActiveTailoringRunStatus(latestRun.status);

  function launch(): void {
    // `isPending` lags a same-tick double click; `isMutating` does not (H-58, CLAUDE.md).
    if (
      input === null ||
      queryClient.isMutating({ mutationKey: createTailoringRunMutationKey(map) }) > 0
    ) {
      return;
    }
    create.mutate(input, {
      onSuccess: (created) => {
        void navigate(runLink(map, created.id));
      },
    });
  }

  const rejection = create.error;
  const activeRunId = rejection === null ? null : activeTailoringRunId(rejection);

  return (
    <>
      <p className="mb-6 text-sm text-slate-700">{ACCOUNT_PROMISE}</p>

      <GuestWorkOffer userId={userId} />

      <Section id="account-base-cv-heading" title="Your base CV">
        <SavedBaseCvPicker mode="select" chosenId={chosenId} onChoose={setChosenId} />
      </Section>

      <Section id="account-job-posting-heading" title="The job you are applying for">
        <JobPostingPanel />
      </Section>

      <Section id="account-tailoring-heading" title="Your tailored CV and cover letter">
        <div className="space-y-4">
          {latest.isError && latest.data === undefined ? (
            <div role="alert" className="space-y-2 text-sm">
              <p className="text-slate-700">{LATEST_RUN_ERROR_NOTE}</p>
              <button
                type="button"
                onClick={() => {
                  void latest.refetch();
                }}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 font-medium text-slate-800"
              >
                {RETRY_LABEL}
              </button>
            </div>
          ) : (
            <LatestRunCard run={latestRun} />
          )}

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
              message={rejectionMessage(rejection, 'account')}
              onViewActiveRun={null}
              activeRunHref={activeRunId === null ? null : runLink(map, activeRunId)}
            />
          )}
        </div>
      </Section>
    </>
  );
}
