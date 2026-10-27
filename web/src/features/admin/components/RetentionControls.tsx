import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { ConfirmDeleteDialog } from '@/components/ui/ConfirmDeleteDialog';

import { CANCEL_LABEL, PREVIEWING_LABEL, PREVIEW_FAILED, RUNNING_LABEL } from '../adminCopy';

import type { UseMutationResult, UseQueryResult } from '@tanstack/react-query';

export interface RetentionControlsProps<T> {
  readonly preview: UseQueryResult<T>;
  readonly run: UseMutationResult<T, Error, void>;
  readonly runMutationKey: readonly string[];
  readonly previewLabel: string;
  readonly runLabel: string;
  readonly dialogTitle: string;
  /** Built from the preview when there is one. */
  readonly dialogMessage: string;
  readonly previewSentence: (report: T) => string;
  readonly reportSentence: (report: T) => string;
  /** A refused run, in words — never a success sentence (AC-37). */
  readonly failureCopy: (error: Error) => string;
}

/**
 * *Preview* and *Run* for one retention job (AC-37, AC-38). The preview is a read the click asks
 * for; the run confirms first, is never re-sent by itself (ADR-0031) and a same-tick double
 * confirm is stopped by `isMutating` (2.1). Not optimistic: the report is the server's.
 */
export function RetentionControls<T>({
  preview,
  run,
  runMutationKey,
  previewLabel,
  runLabel,
  dialogTitle,
  dialogMessage,
  previewSentence,
  reportSentence,
  failureCopy,
}: RetentionControlsProps<T>): React.JSX.Element {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);

  function confirm(): void {
    setConfirming(false);
    if (queryClient.isMutating({ mutationKey: runMutationKey }) > 0) return;
    run.mutate();
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          onClick={() => {
            void preview.refetch();
          }}
          disabled={preview.isFetching}
          className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60"
        >
          {preview.isFetching ? PREVIEWING_LABEL : previewLabel}
        </button>
        <button
          type="button"
          onClick={() => {
            setConfirming(true);
          }}
          disabled={run.isPending}
          className="rounded-md border border-danger-200 bg-white px-4 py-2 text-sm font-semibold text-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60"
        >
          {run.isPending ? RUNNING_LABEL : runLabel}
        </button>
      </div>
      {preview.data !== undefined ? (
        <p role="status" className="text-sm text-slate-700">
          {previewSentence(preview.data)}
        </p>
      ) : (
        preview.error !== null && (
          <p role="alert" className="text-sm text-danger-600">
            {PREVIEW_FAILED}
          </p>
        )
      )}
      {confirming && (
        <ConfirmDeleteDialog
          title={dialogTitle}
          message={dialogMessage}
          confirmLabel={runLabel}
          cancelLabel={CANCEL_LABEL}
          onConfirm={confirm}
          onCancel={() => {
            setConfirming(false);
          }}
        />
      )}
      {run.data !== undefined && (
        <p role="status" className="text-sm text-slate-800">
          {reportSentence(run.data)}
        </p>
      )}
      {run.error !== null && (
        <p role="alert" className="text-sm text-danger-600">
          {failureCopy(run.error)}
        </p>
      )}
    </div>
  );
}
