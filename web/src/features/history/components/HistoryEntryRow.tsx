import { useId } from 'react';
import { Link } from 'react-router';

import { formatStoredUntil } from '@/features/intake/format';
import { isActiveTailoringRunStatus } from '@/features/tailoring/types';

import {
  CV_DELETED_NOTE,
  DELETE_ENTRY_FAILED_NOTE,
  DELETE_ENTRY_IN_PROGRESS_NOTE,
  DELETE_ENTRY_LABEL,
  DELETE_ENTRY_PENDING_LABEL,
  EDITED_BADGE,
  OPEN_ENTRY_LABEL,
  POSTING_MISSING_NOTE,
  historyStatusCopy,
} from '../historyCopy';

import type { HistoryEntry } from '../types';

/**
 * Where one row's deletion stands — owned by the page (it owns the mutation and the dialog), shown
 * by the row. A union rather than two booleans, so "deleting" and "failed" cannot both be true.
 *
 * `in_progress` is the 409 `tailoring_run_in_progress`; `unavailable` a 503 or no answer (kept).
 */
export type HistoryEntryDeletion =
  | { readonly kind: 'idle' }
  | { readonly kind: 'deleting' }
  | { readonly kind: 'failed'; readonly reason: 'in_progress' | 'unavailable' };

export interface HistoryEntryRowProps {
  readonly entry: HistoryEntry;
  readonly deletion: HistoryEntryDeletion;
  /** Opens the confirmation — the row never deletes anything itself. */
  readonly onDelete: () => void;
}

const LINK_CLASS = 'font-medium text-slate-900 underline underline-offset-2';

/**
 * One history entry (AC-44) — **presentational**: the posting's title, else its preview; the CV's
 * label, else its filename, else *"CV deleted"*; the status in words (incl. `base_cv_deleted`); the
 * requested date; the *Edited* badge; **Open** (a link to `/history/{id}`) and **Delete**.
 *
 * Delete is disabled on a `queued`/`running` entry with the reason beside it (AC-45): there is no
 * cancellation, and the server would answer 409 anyway — the client does not decide *whether* an
 * entry may go, it only avoids offering a request it has just been told will be refused. Every
 * string from the entry is user or model-adjacent text, rendered as text.
 */
export function HistoryEntryRow({
  entry,
  deletion,
  onDelete,
}: HistoryEntryRowProps): React.JSX.Element {
  const reasonId = useId();
  const stillRunning = isActiveTailoringRunStatus(entry.status);
  const note =
    stillRunning || (deletion.kind === 'failed' && deletion.reason === 'in_progress')
      ? DELETE_ENTRY_IN_PROGRESS_NOTE
      : deletion.kind === 'failed'
        ? DELETE_ENTRY_FAILED_NOTE
        : null;
  const posting =
    entry.posting === null ? POSTING_MISSING_NOTE : (entry.posting.title ?? entry.posting.preview);
  const cv =
    entry.base_cv === null
      ? CV_DELETED_NOTE
      : (entry.base_cv.label ?? entry.base_cv.original_filename);

  return (
    <li className="space-y-2 rounded-lg border border-slate-200 p-4">
      <div className="flex items-baseline justify-between gap-3">
        <p className="font-medium text-slate-900">{posting}</p>
        {entry.edited && (
          <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
            {EDITED_BADGE}
          </span>
        )}
      </div>
      <p className="text-sm text-slate-600">
        <span className="text-slate-500">CV: </span>
        <span>{cv}</span>
      </p>
      <p className="text-sm text-slate-700">{historyStatusCopy(entry)}</p>
      <p className="text-xs text-slate-500">
        Requested <time dateTime={entry.requested_at}>{formatStoredUntil(entry.requested_at)}</time>
      </p>
      <div className="flex items-center gap-4 text-sm">
        <Link to={`/history/${encodeURIComponent(entry.id)}`} className={LINK_CLASS}>
          {OPEN_ENTRY_LABEL}
        </Link>
        <button
          type="button"
          onClick={onDelete}
          disabled={stillRunning || deletion.kind === 'deleting'}
          aria-describedby={note === null ? undefined : reasonId}
          className="text-red-700 underline underline-offset-2 disabled:text-slate-400 disabled:no-underline"
        >
          {deletion.kind === 'deleting' ? DELETE_ENTRY_PENDING_LABEL : DELETE_ENTRY_LABEL}
        </button>
      </div>
      {note !== null && (
        <p
          id={reasonId}
          role={deletion.kind === 'failed' ? 'alert' : undefined}
          className="text-sm text-slate-600"
        >
          {note}
        </p>
      )}
    </li>
  );
}
