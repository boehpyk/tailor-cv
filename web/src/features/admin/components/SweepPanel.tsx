import { ApiError } from '@/api/client';

import {
  PREVIEW_SWEEP_LABEL,
  RUN_SWEEP_DIALOG_MESSAGE,
  RUN_SWEEP_DIALOG_TITLE,
  RUN_SWEEP_LABEL,
  SWEEP_CHECK_UNAVAILABLE,
  SWEEP_FAILED,
  SWEEP_HEADING,
  sweepPreviewSentence,
  sweepReportSentence,
} from '../adminCopy';
import { retentionRunMutationKey, useRetentionRuns } from '../hooks/useRetentionRuns';

import { RetentionControls } from './RetentionControls';

export interface SweepPanelProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

function failureCopy(error: Error): string {
  return error instanceof ApiError && error.code === 'orphan_check_unavailable'
    ? SWEEP_CHECK_UNAVAILABLE
    : SWEEP_FAILED;
}

/** The orphan sweep (AC-38): *Preview sweep*, *Run sweep* behind a dialog, the 503 in words. */
export function SweepPanel({ actorId }: SweepPanelProps): React.JSX.Element {
  const { sweepPreview, sweep } = useRetentionRuns(actorId);
  return (
    <section aria-labelledby="admin-sweep-heading" className="space-y-3">
      <h2 id="admin-sweep-heading" className="text-base font-semibold text-slate-900">
        {SWEEP_HEADING}
      </h2>
      <RetentionControls
        preview={sweepPreview}
        run={sweep}
        runMutationKey={retentionRunMutationKey('sweep')}
        previewLabel={PREVIEW_SWEEP_LABEL}
        runLabel={RUN_SWEEP_LABEL}
        dialogTitle={RUN_SWEEP_DIALOG_TITLE}
        dialogMessage={RUN_SWEEP_DIALOG_MESSAGE}
        previewSentence={sweepPreviewSentence}
        reportSentence={sweepReportSentence}
        failureCopy={failureCopy}
      />
    </section>
  );
}
