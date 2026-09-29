import type { HistoryEntry } from '../types';

/**
 * Where one row's deletion stands — owned by the page (it owns the mutation and the dialog), shown
 * by the row. A union rather than two booleans, so "deleting" and "failed" cannot both be true.
 *
 * `in_progress` is the 409 `tailoring_run_in_progress`; `unavailable` a 503 (kept, try again).
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

/**
 * One history entry (AC-44) — **presentational**: the posting's title, else its preview; the CV's
 * label, else its filename, else *"CV deleted"*; the status copy (incl. `base_cv_deleted`); the
 * requested date; the *Edited* badge; **Open** (a link to `/history/{id}`) and **Delete**, disabled
 * with its reason on a `queued`/`running` row (AC-45). Every string from the entry is user or
 * model-adjacent text and is rendered as text.
 *
 * SKELETON (T29): a distinguishable stub; T31 renders the row.
 */
export function HistoryEntryRow({ entry }: HistoryEntryRowProps): React.JSX.Element {
  return <li data-history-entry={entry.id}>HistoryEntryRow (skeleton)</li>;
}
