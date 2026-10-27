import { ApiError } from '@/api/client';
import { describeAge } from '@/features/health/purgeView';
import { useReadiness } from '@/features/health/hooks/useReadiness';

import {
  OVERDUE_UNKNOWN,
  PREVIEW_PURGE_LABEL,
  PURGE_FAILED,
  PURGE_HEADING,
  PURGE_IN_PROGRESS,
  PURGE_NEVER_RUN,
  PURGE_NOT_SCHEDULED,
  PURGE_OUTCOME_LABELS,
  PURGE_SCHEDULED,
  PURGE_STALE,
  RUN_PURGE_DIALOG_TITLE,
  RUN_PURGE_LABEL,
  overdueLine,
  purgeLastRun,
  purgePreviewSentence,
  purgeReportSentence,
  runPurgeDialogMessage,
} from '../adminCopy';
import { retentionRunMutationKey, useRetentionRuns } from '../hooks/useRetentionRuns';

import { RetentionControls } from './RetentionControls';

import type { GuestPurgeStatus } from '@/api/health';

export interface PurgePanelProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

function failureCopy(error: Error): string {
  return error instanceof ApiError && error.code === 'purge_in_progress'
    ? PURGE_IN_PROGRESS
    : PURGE_FAILED;
}

/**
 * The guest purge (AC-37): the job's facts from `useReadiness` (none while it loads or fails —
 * `JobHealth` words that), *Preview purge*, and *Run purge* behind a dialog naming the preview's
 * numbers or the batch bound. A 409 is never worded as success. A dry run reports
 * `sessions_deleted: 0`; its would-delete count is `examined` (AC-16).
 */
export function PurgePanel({ actorId }: PurgePanelProps): React.JSX.Element {
  const job = useReadiness().data?.jobs?.guest_purge;
  const { purgePreview, purge } = useRetentionRuns(actorId);
  const preview = purgePreview.data;
  return (
    <section aria-labelledby="admin-purge-heading" className="space-y-3">
      <h2 id="admin-purge-heading" className="font-display text-base font-bold text-slate-900">
        {PURGE_HEADING}
      </h2>
      {job !== undefined && <PurgeFacts job={job} />}
      <RetentionControls
        preview={purgePreview}
        run={purge}
        runMutationKey={retentionRunMutationKey('purge')}
        previewLabel={PREVIEW_PURGE_LABEL}
        runLabel={RUN_PURGE_LABEL}
        dialogTitle={RUN_PURGE_DIALOG_TITLE}
        dialogMessage={runPurgeDialogMessage(
          preview === undefined
            ? null
            : { sessions: preview.examined, files: preview.files_unlinked },
        )}
        previewSentence={(report) => purgePreviewSentence(report.examined, report.files_unlinked)}
        reportSentence={purgeReportSentence}
        failureCopy={failureCopy}
      />
    </section>
  );
}

function PurgeFacts({ job }: { readonly job: GuestPurgeStatus }): React.JSX.Element {
  return (
    <ul className="space-y-1 text-sm text-slate-700">
      <li>{job.scheduled ? PURGE_SCHEDULED : PURGE_NOT_SCHEDULED}</li>
      <li>
        {job.last_run_age_seconds === null || job.last_outcome === null
          ? PURGE_NEVER_RUN
          : purgeLastRun(
              describeAge(job.last_run_age_seconds).replace(/ ago$/, ''),
              PURGE_OUTCOME_LABELS[job.last_outcome],
            )}
      </li>
      <li>{job.overdue === null ? OVERDUE_UNKNOWN : overdueLine(job.overdue)}</li>
      {job.stale && <li className="font-medium text-danger-600">{PURGE_STALE}</li>}
    </ul>
  );
}
