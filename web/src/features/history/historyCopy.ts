/**
 * Every sentence the history surface says (plan §7), from the spec's AC-44, AC-45 and AC-49 — one
 * module, so a reworded line changes in one place and a test can name the constant it asserts.
 *
 * SKELETON (T29): the sentences the spec fixes verbatim. Status copy per status and per failure
 * reason (incl. `base_cv_deleted`) and the page's deletion note arrive with T31.
 */

export const HISTORY_HEADING = 'Your history';
export const HISTORY_LOADING_NOTE = 'Loading your history…';
export const HISTORY_LOAD_ERROR_NOTE = "Couldn't load your history";
export const HISTORY_EMPTY_NOTE = 'Nothing tailored yet';
export const RETRY_LABEL = 'Retry';
export const LOAD_MORE_LABEL = 'Load more';
export const OPEN_ENTRY_LABEL = 'Open';
export const DELETE_ENTRY_LABEL = 'Delete';
export const EDITED_BADGE = 'Edited';
/** The CV cell of an entry whose saved CV has since been deleted (`base_cv === null`, H-29). */
export const CV_DELETED_NOTE = 'CV deleted';

export const DELETE_ENTRY_DIALOG_TITLE = 'Delete tailored application';
export const DELETE_ENTRY_MESSAGE =
  "Delete this tailored application? This removes its tailored CV and cover letter, any files you exported, and the job posting if nothing else uses it. This can't be undone.";
export const DELETE_ENTRY_PENDING_LABEL = 'Deleting…';
/** 409 `tailoring_run_in_progress`, and the disabled Delete's reason on a `queued`/`running` row. */
export const DELETE_ENTRY_IN_PROGRESS_NOTE = 'Still tailoring — you can delete it when it finishes';
/** 503 — the entry was kept. */
export const DELETE_ENTRY_FAILED_NOTE = 'Not deleted — try again';
