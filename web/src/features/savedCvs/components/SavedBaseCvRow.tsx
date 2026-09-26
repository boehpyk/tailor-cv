import { formatSize } from '@/features/intake/format';

import {
  DELETE_LABEL,
  DELETE_PENDING_LABEL,
  RENAME_LABEL,
  SAVED_CV_STATUS_LABELS,
} from '../savedCvsCopy';

import type { SavedBaseCv } from '../types';
import type { ReactNode } from 'react';

export interface SavedBaseCvRowProps {
  readonly cv: SavedBaseCv;
  /**
   * A delete of this row is on the wire: the row reads "Deleting…" and its actions are disabled.
   * The row stays until the server answers — there is no optimistic removal (AC-36).
   */
  readonly isDeleting: boolean;
  /** Another delete is on the wire: this row's Delete waits for it (one irreversible thing at a time). */
  readonly deleteDisabled: boolean;
  /** The row's own notice — a delete that did not happen ("Not deleted — try again") — or `null`. */
  readonly notice: string | null;
  /** Rendered in place of the name while this row is being renamed; the section decides which row. */
  readonly renameForm: ReactNode;
  readonly onRename: () => void;
  readonly onDelete: () => void;
}

/**
 * "20 Sep 2026", in UTC — the instant is the server's whole-second UTC, and formatting it in UTC
 * keeps a user near midnight from reading a different day than the one the list is ordered by.
 */
const uploadedFormat = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});

/**
 * One saved CV, presentational (AC-34): its display name (the label, else the filename), the
 * filename as well when it is labelled, its status — with the server's `failure_message` for
 * `extraction_failed` (AC-35) — its size, the date it was uploaded, and **Rename** and **Delete**.
 *
 * Every string that came from a user — the label, the filename — is a text node (AC-47). Each fact
 * keeps its own element, as `BaseCvCard` does, so it can be found and read on its own.
 */
export function SavedBaseCvRow({
  cv,
  isDeleting,
  deleteDisabled,
  notice,
  renameForm,
  onRename,
  onDelete,
}: SavedBaseCvRowProps): React.JSX.Element {
  const busy = isDeleting || renameForm !== null;

  return (
    <li className="rounded-lg border border-slate-200 p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1 space-y-1">
          {renameForm ?? (
            <>
              <p className="font-medium break-words text-slate-900">
                {cv.label ?? cv.original_filename}
              </p>
              {cv.label !== null && (
                <p className="text-sm break-words text-slate-600">{cv.original_filename}</p>
              )}
            </>
          )}
          <p className="text-sm text-slate-500">{SAVED_CV_STATUS_LABELS[cv.status]}</p>
          {cv.status === 'extraction_failed' && cv.failure_message !== null && (
            <p className="text-sm text-amber-800">{cv.failure_message}</p>
          )}
          <p className="text-sm text-slate-500">{formatSize(cv.size_bytes)}</p>
          <p className="text-sm text-slate-500">
            Uploaded {uploadedFormat.format(new Date(cv.uploaded_at))}
          </p>
          {notice !== null && (
            <p role="alert" className="text-sm text-red-700">
              {notice}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-2">
          {isDeleting ? (
            <p role="status" className="text-sm font-medium text-slate-500">
              {DELETE_PENDING_LABEL}
            </p>
          ) : (
            <>
              <button
                type="button"
                onClick={onRename}
                disabled={busy}
                className="text-sm font-medium text-slate-600 hover:text-slate-900 disabled:opacity-60"
              >
                {RENAME_LABEL}
              </button>
              <button
                type="button"
                onClick={onDelete}
                disabled={busy || deleteDisabled}
                className="text-sm font-medium text-red-700 hover:text-red-900 disabled:opacity-60"
              >
                {DELETE_LABEL}
              </button>
            </>
          )}
        </div>
      </div>
    </li>
  );
}
