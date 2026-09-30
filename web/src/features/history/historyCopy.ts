import { failureCopyFor } from '@/features/tailoring/failureCopy';

import type { HistoryEntry } from './types';

/**
 * Every sentence the history surface says (plan §7), from the spec's AC-44, AC-45 and AC-49 — one
 * module, so a reworded line changes in one place and a test can name the constant it asserts.
 */

export const HISTORY_HEADING = 'Your history';
/**
 * AC-49: the history page states how deletion works — what goes with an entry, and what does not.
 * No "24 hours" anywhere in account scope: nothing here is on a timer.
 */
export const HISTORY_DELETION_NOTE =
  "Everything here is kept until you delete it. Deleting an entry removes its tailored CV and cover letter and any files you exported; your saved CVs aren't affected.";
export const HISTORY_LOADING_NOTE = 'Loading your history…';
export const HISTORY_LOAD_ERROR_NOTE = "Couldn't load your history";
export const HISTORY_EMPTY_NOTE = 'Nothing tailored yet';
export const HISTORY_EMPTY_ACTION = 'Go to your workspace';
export const RETRY_LABEL = 'Retry';
export const LOAD_MORE_LABEL = 'Load more';
export const LOAD_MORE_PENDING_LABEL = 'Loading more…';
/** H-59: a later page failed; the loaded rows stay and Retry asks for that page again. */
export const LOAD_MORE_ERROR_NOTE = "Couldn't load more.";
export const OPEN_ENTRY_LABEL = 'Open';
export const DELETE_ENTRY_LABEL = 'Delete';
export const EDITED_BADGE = 'Edited';
/** The CV cell of an entry whose saved CV has since been deleted (`base_cv === null`, H-29). */
export const CV_DELETED_NOTE = 'CV deleted';
/** The posting cell when the posting row is missing — should be impossible (H-30), still handled. */
export const POSTING_MISSING_NOTE = 'Job posting unavailable';

export const DELETE_ENTRY_DIALOG_TITLE = 'Delete tailored application';
export const DELETE_ENTRY_MESSAGE =
  "Delete this tailored application? This removes its tailored CV and cover letter, any files you exported, and the job posting if nothing else uses it. This can't be undone.";
export const DELETE_ENTRY_CONFIRM_LABEL = 'Delete';
export const DELETE_ENTRY_CANCEL_LABEL = 'Cancel';
export const DELETE_ENTRY_PENDING_LABEL = 'Deleting…';
/** 409 `tailoring_run_in_progress`, and the disabled Delete's reason on a `queued`/`running` row. */
export const DELETE_ENTRY_IN_PROGRESS_NOTE = 'Still tailoring — you can delete it when it finishes';
/** 503 (or no answer) — the entry was kept. */
export const DELETE_ENTRY_FAILED_NOTE = 'Not deleted — try again';
/** 404 on delete: another tab got there first. A note, not an error (AC-45). */
export const DELETE_ENTRY_ALREADY_GONE_NOTE = 'That entry was already deleted.';

/**
 * An entry's status, in words. A failure is 1.3's own copy for its reason (`failureCopy.ts`) —
 * including H-22's `base_cv_deleted` — so a failed run reads the same here as on its page.
 */
export function historyStatusCopy(entry: HistoryEntry): string {
  switch (entry.status) {
    case 'queued':
      return 'Waiting for a worker…';
    case 'running':
      return 'Tailoring…';
    case 'succeeded':
      return 'Ready';
    case 'failed':
      return failureCopyFor(entry.failure_reason).headline;
  }
}
