/**
 * Every sentence the admin screen puts on screen (slice 4.1, AC-31, AC-33; slice 4.2, AC-31…AC-35),
 * in one module so a test imports the string it pins instead of retyping it. The not-found view is
 * `NotFoundPage`'s own copy (OQ-2): nothing here says "forbidden".
 */

import type { AccountCounts } from '@/api/admin';

/** AC-31: the probe is pending (`role="status"`). */
export const ADMIN_CHECKING = 'Checking access…';
/** AC-31: 503 or a network failure (`role="alert"`), visibly different from checking. */
export const ADMIN_UNAVAILABLE = "Couldn't check admin access.";
export const ADMIN_RETRY_LABEL = 'Retry';

/**
 * AC-33's three sentences live in `lazyCopy.ts`, which the main bundle imports (`AdminRoute`,
 * `LazyBoundary`). Re-exported here so a test reads every admin sentence from one module; the main
 * bundle must never import *this* module, or Rollup puts the whole of it in the main chunk (AC-40).
 */
export { LAZY_CHUNK_FAILED, LAZY_LOADING, LAZY_RELOAD_LABEL } from './lazyCopy';

// --- Slice 4.2: the user list (AC-31, AC-32) ------------------------------------------------------

/** The list page's `<h1>`, which labels its `<section>` (4.1's shell heading *Admin* retired). */
export const USERS_HEADING = 'Users';
/** The table's `<caption>` (AC-39). */
export const USERS_CAPTION = 'Registered accounts, newest first';
export const USERS_LOADING = 'Loading users…';
export const USERS_ERROR = "Couldn't load users.";
/** Never expected (the actor is a user), but stated rather than an empty table. */
export const USERS_EMPTY = 'No users yet.';
export function usersNoMatch(q: string): string {
  return `No users match “${q}”.`;
}
export const LOAD_MORE_LABEL = 'Load more';
export const LOADING_MORE_LABEL = 'Loading more…';

export const SEARCH_LABEL = 'Search by email';
export const SEARCH_SUBMIT_LABEL = 'Search';
/** A 422 `invalid_search`; the typed text stays in the field. */
export const SEARCH_INVALID = "That search can't be used.";

export const COLUMN_EMAIL = 'Email';
export const COLUMN_ROLE = 'Role';
export const COLUMN_STATUS = 'Status';
export const COLUMN_CREATED = 'Created';

export const STATUS_ACTIVE = 'Active';
export const STATUS_DISABLED = 'Disabled';
/** The actor's own row (OQ-18). */
export const YOU_MARKER = 'You';
export const ROLE_LABELS = { user: 'User', admin: 'Admin' } as const;

/** AC-35: the list's notice after an erase, from `location.state`. */
export function erasedNotice(email: string): string {
  return `Erased the account ${email}.`;
}
export function unlinkFailuresNote(failures: number): string {
  return `${String(failures)} file(s) could not be removed; the orphan sweep will reclaim them.`;
}

// --- Slice 4.2: one account (AC-33) ---------------------------------------------------------------

export const USER_LOADING = 'Loading…';
/** 404 `user_not_found` — also a disable/enable/erase that found it gone. */
export const USER_GONE = 'This account no longer exists.';
export const BACK_TO_USERS = 'Back to users';
export const USER_ERROR = "Couldn't load this account.";
export const ROLE_READ_ONLY_NOTE = "Roles change only from the server's command line.";

export const FIELD_ID = 'ID';
export const FIELD_ROLE = 'Role';
export const FIELD_CREATED = 'Created';
export const FIELD_STATUS = 'Status';
export const COUNTS_HEADING = 'What this account holds';
/** The seven counts, in display order; `logins` is *Signed-in sessions* (OQ-8). */
export const COUNT_LABELS: Readonly<Record<keyof AccountCounts, string>> = {
  base_cvs: 'Saved CVs',
  files: 'Files',
  logins: 'Signed-in sessions',
  tailoring_runs: 'Tailoring runs',
  job_postings: 'Job postings',
  export_jobs: 'Exports',
  tracked_applications: 'Board cards',
};

// --- Slice 4.2: actions (AC-34, AC-35, OQ-12, OQ-18) ----------------------------------------------

export const DISABLE_LABEL = 'Disable';
export const DISABLE_PENDING_LABEL = 'Disabling…';
export const ENABLE_LABEL = 'Enable';
export const ENABLE_PENDING_LABEL = 'Enabling…';
export const ERASE_LABEL = 'Erase';
export const ERASE_PENDING_LABEL = 'Erasing…';
export const CANCEL_LABEL = 'Cancel';

/** OQ-18: added to either dialog on the actor's own account. */
export const OWN_ACCOUNT_NOTE = 'This is your account; you will be signed out.';

export const DISABLE_DIALOG_TITLE = 'Disable this account?';
export function disableDialogMessage(email: string, ownAccount: boolean): string {
  const message = `${email} will be signed out everywhere and cannot log in until the account is enabled again. Their data is kept.`;
  return ownAccount ? `${message} ${OWN_ACCOUNT_NOTE}` : message;
}

export const ERASE_DIALOG_TITLE = 'Erase this account?';
export function eraseDialogMessage(
  email: string,
  counts: AccountCounts,
  ownAccount: boolean,
): string {
  const held = (Object.keys(COUNT_LABELS) as (keyof AccountCounts)[])
    .map((key) => `${COUNT_LABELS[key]}: ${String(counts[key])}`)
    .join(', ');
  const message = `${email} and everything it holds will be erased (${held}). This cannot be undone.`;
  return ownAccount ? `${message} ${OWN_ACCOUNT_NOTE}` : message;
}

/** A refused action other than 404; the button is usable again. Writes hold (ADR-0031). */
export const ACTION_FAILED = {
  disable: "Couldn't disable this account. Try again.",
  enable: "Couldn't enable this account. Try again.",
  erase: "Couldn't erase this account. Try again.",
} as const;

// --- Slice 4.3: an account's content (AC-25…AC-29) ------------------------------------------------
// Reused from above: ADMIN_RETRY_LABEL, LOAD_MORE_LABEL, LOADING_MORE_LABEL, USER_GONE,
// BACK_TO_USERS, CANCEL_LABEL, unlinkFailuresNote.

/** AC-26: the three sections on `/admin/users/:userId`, below the actions. */
export const CONTENT_HEADINGS = {
  cvs: 'Saved CVs',
  history: 'History',
  postings: 'Job postings',
} as const;
/**
 * Each section's own loading line (`role="status"`), per section (owner's AC-26 amendment,
 * 2026-10-12): it shares the account page with the overview's *Loading…*, and three identical
 * lines beside it could not be told apart. A read page's loading line is `USER_LOADING`.
 */
export const CONTENT_LOADING = {
  cvs: 'Loading saved CVs…',
  history: 'Loading history…',
  postings: 'Loading job postings…',
} as const;
export const CONTENT_EMPTY = {
  cvs: 'No saved CVs.',
  history: 'No tailoring history.',
  postings: 'No job postings.',
} as const;
/** `role="alert"` with Retry, visibly distinct from loading. */
export const CONTENT_ERROR = {
  cvs: "Couldn't load saved CVs.",
  history: "Couldn't load the history.",
  postings: "Couldn't load job postings.",
} as const;
/** OQ-6: the postings section holds the 20 most recent and says so when full. */
export const POSTINGS_WINDOW_NOTE = 'Showing the 20 most recent.';
export const UNTITLED_POSTING = 'Untitled job posting';
/**
 * A run reads as its history row does (AC-28): status words, *CV deleted*, a missing posting. The
 * history feature is in the main chunk, so re-exporting its copy costs the admin chunk nothing.
 */
export {
  CV_DELETED_NOTE,
  POSTING_MISSING_NOTE,
  historyStatusCopy,
} from '@/features/history/historyCopy';

/** A read page's link back to the account it belongs to. */
export const BACK_TO_ACCOUNT = 'Back to the account';

// CV page (AC-27)
export const CV_FIELD_FILENAME = 'Filename';
export const CV_FIELD_TYPE = 'Type';
export const CV_FIELD_SIZE = 'Size';
export const CV_FIELD_UPLOADED = 'Uploaded';
export const CV_FIELD_CHARACTERS = 'Characters';
export const CV_TEXT_HEADING = 'Extracted text';
export function cvExtractionFailed(message: string): string {
  return `Text could not be extracted: ${message}`;
}
/** `status: 'uploaded'` — extraction has not run yet, so there is neither text nor a failure. */
export const CV_NOT_YET_EXTRACTED = 'The text has not been extracted yet.';
/** 404 `base_cv_not_found`. */
export const CV_GONE = 'This CV no longer exists.';
export const CV_ERROR = "Couldn't load this CV.";
/** The run page's CV cell when the CV list itself failed — not evidence the CV was deleted. */
export const RUN_CV_UNAVAILABLE = "Couldn't load the CV.";

// Posting page (AC-25)
export const POSTING_FIELD_SOURCE = 'Source';
export const POSTING_FIELD_URL = 'URL';
export const POSTING_FIELD_CAPTURED = 'Captured';
export const POSTING_SOURCE_LABELS = { pasted: 'Pasted', fetched: 'Fetched from a link' } as const;
export const POSTING_TEXT_HEADING = 'Posting text';
/** 404 `job_posting_not_found`. */
export const POSTING_GONE = 'This job posting no longer exists.';
export const POSTING_ERROR = "Couldn't load this job posting.";

// Run page (AC-28)
export const RUN_HEADING = 'Tailoring run';
export const RUN_FIELD_STATUS = 'Status';
export const RUN_FIELD_POSTING = 'Job posting';
export const RUN_FIELD_CV = 'CV';
export const RUN_FIELD_REQUESTED = 'Requested';
export const DOCUMENT_HEADINGS = { cv: 'Tailored CV', cover_letter: 'Cover letter' } as const;
export const RUN_STILL_RUNNING = 'Still running — the documents appear when it finishes.';
export const RUN_REFRESH_LABEL = 'Refresh';
/** 404 `tailoring_run_not_found`. */
export const RUN_GONE = 'This run no longer exists.';
export const RUN_ERROR = "Couldn't load this run.";
export const DOWNLOAD_INLINE_LABELS = { md: 'Download .md', txt: 'Download .txt' } as const;
export const EXPORTS_HEADING = 'Exports';
export const EXPORTS_EMPTY = 'No exports.';
export const EXPORTS_ERROR = "Couldn't load the exports.";
export const EXPORT_DOWNLOAD_LABEL = 'Download';
export const EXPORT_OLDER_VERSION = 'older version';
export const EXPORT_STATUS_LABELS = {
  queued: 'Queued',
  rendering: 'Rendering',
  ready: 'Ready',
  failed: 'Failed',
} as const;
/** 410 `export_file_gone`. */
export const DOWNLOAD_FILE_GONE = 'That file is no longer on the server.';
export const DOWNLOAD_FAILED = "Couldn't download this file. Try again.";

// Deletes (AC-29)
export const DELETE_CV_LABEL = 'Delete CV';
export const DELETE_ENTRY_LABEL = 'Delete history entry';
export const DELETE_PENDING_LABEL = 'Deleting…';
export const DELETE_CV_DIALOG_TITLE = 'Delete this CV?';
export const DELETE_ENTRY_DIALOG_TITLE = 'Delete this history entry?';
export function deleteCvDialogMessage(name: string): string {
  return `${name} and its file will be deleted. This cannot be undone.`;
}
export function deleteEntryDialogMessage(postingTitle: string, requestedOn: string): string {
  return `The entry for ${postingTitle} from ${requestedOn} will be deleted, with its exports and its card on the board. This cannot be undone.`;
}
/** The account page's notice after a delete, from `location.state`. */
export function deletedNotice(name: string): string {
  return `Deleted ${name}.`;
}
/** 409 `tailoring_run_in_progress`: the dialog closes, nothing changed. */
export const DELETE_IN_PROGRESS = 'This run is still working. Try again when it finishes.';
/** The kind's 404. */
export const DELETE_ALREADY_GONE = 'Already deleted.';
export const DELETE_FAILED = "Couldn't delete. Try again.";

// --- Slice 4.4: guest sessions, storage and retention runs (AC-31…AC-39) --------------------------
// Reused from above: ADMIN_RETRY_LABEL, LOAD_MORE_LABEL, LOADING_MORE_LABEL, CANCEL_LABEL,
// DELETE_PENDING_LABEL, CONTENT_HEADINGS.postings, unlinkFailuresNote.

/** AC-31: the admin nav, *Users* · *Guests* · *Storage*. */
export const ADMIN_NAV_LABEL = 'Admin';
export const NAV_LABELS = { users: 'Users', guests: 'Guests', storage: 'Storage' } as const;

// The guest list (AC-32)
export const GUESTS_HEADING = 'Guest sessions';
export const GUESTS_CAPTION = 'Live guest sessions, latest expiry first';
export const GUESTS_LOADING = 'Loading guest sessions…';
export const GUESTS_ERROR = "Couldn't load guest sessions.";
export const GUESTS_EMPTY = 'No live guest sessions.';
export const COLUMN_STARTED = 'Started';
export const COLUMN_EXPIRES = 'Expires at';
/** The four counts a session row and its overview show, in display order. */
export const GUEST_COUNT_LABELS = {
  base_cvs: 'CVs',
  job_postings: 'Job postings',
  tailoring_runs: 'Tailoring runs',
  export_jobs: 'Exports',
} as const;

// One session (AC-33)
export const GUEST_SESSION_HEADING = 'Guest session';
export const GUEST_SESSION_LOADING = 'Loading guest session…';
export const GUEST_SESSION_ERROR = "Couldn't load this guest session.";
/** 404 `guest_session_not_found` — on the session page and on its read pages (AC-33, AC-35). */
export const GUEST_SESSION_GONE =
  'This guest session no longer exists — it expired, was kept in an account, or was deleted.';
export const BACK_TO_GUESTS = 'Back to guest sessions';
/** A guest read page's link back to its session (T18's carry-over). */
export const BACK_TO_GUEST_SESSION = 'Back to the guest session';
export const GUEST_SECTION_HEADINGS = {
  cvs: 'CVs',
  postings: 'Job postings',
  runs: 'Tailoring runs',
} as const;
export const GUEST_SECTION_EMPTY = {
  cvs: 'No CVs.',
  postings: 'No job postings.',
  runs: 'No tailoring runs.',
} as const;

// Delete a guest session (AC-34)
export const DELETE_GUEST_LABEL = 'Delete guest session';
export const DELETE_GUEST_DIALOG_TITLE = 'Delete this guest session?';
export function deleteGuestDialogMessage(
  counts: Readonly<Record<keyof typeof GUEST_COUNT_LABELS, number>>,
  runInProgress: boolean,
): string {
  const held = (Object.keys(GUEST_COUNT_LABELS) as (keyof typeof GUEST_COUNT_LABELS)[])
    .map((key) => `${GUEST_COUNT_LABELS[key]}: ${String(counts[key])}`)
    .join(', ');
  const message = `The session and everything it holds (${held}) will be deleted before the 24-hour purge. This cannot be undone.`;
  return runInProgress ? `${message} A run still in progress is discarded.` : message;
}
/** The list's notice after an erase; `unlinkFailuresNote` follows when files stayed. */
export function guestDeletedNotice(filesUnlinked: number): string {
  return `Deleted the guest session. ${String(filesUnlinked)} file(s) removed.`;
}
/** AC-34's failure note: *M could not be removed; the orphan sweep will reclaim them.* */
export function guestUnlinkFailuresNote(failures: number): string {
  return `${String(failures)} could not be removed; the orphan sweep will reclaim them.`;
}
export const DELETE_GUEST_FAILED = "Couldn't delete this guest session. Try again.";

// Storage (AC-36)
export const STORAGE_HEADING = 'Storage';
export const STORAGE_LOADING = 'Measuring storage…';
export const STORAGE_ERROR = "Couldn't measure storage.";
export const STORAGE_CAPTION = 'Files on the uploads volume';
export const STORAGE_COLUMN_BUCKET = 'Kind';
export const STORAGE_COLUMN_FILES = 'Files';
export const STORAGE_COLUMN_SIZE = 'Size';
export const STORAGE_BUCKET_LABELS = {
  guest: 'Guest',
  user: 'Account',
  unreferenced: 'Unreferenced',
  partial: 'Partial',
  unrecognized: 'Unrecognized',
} as const;
export const STORAGE_UNREFERENCED_NOTE =
  'The orphan sweep reclaims unreferenced files once they are past its age floor.';
export function storageMeasuredAt(time: string): string {
  return `Measured at ${time}`;
}
export const MEASURE_AGAIN_LABEL = 'Measure again';

// The purge (AC-37)
export const PURGE_HEADING = 'Guest purge';
export const PURGE_SCHEDULED = 'Scheduled';
export const PURGE_NOT_SCHEDULED =
  'Not scheduled — guest data is deleted only when someone runs it';
export const PURGE_NEVER_RUN = 'Never run';
export function purgeLastRun(age: string, outcome: string): string {
  return `Last run ${age} ago: ${outcome}`;
}
export const PURGE_OUTCOME_LABELS = { ok: 'ok', failed: 'failed' } as const;
export function overdueLine(overdue: number): string {
  return `Overdue: ${String(overdue)}`;
}
export const OVERDUE_UNKNOWN = 'Overdue: unknown';
export const PURGE_STALE = 'The purge is stale: it has not run in over three hours.';
export const PREVIEW_PURGE_LABEL = 'Preview purge';
export const RUN_PURGE_LABEL = 'Run purge';
export const RUNNING_LABEL = 'Running…';
export const PREVIEWING_LABEL = 'Previewing…';
export function purgePreviewSentence(sessions: number, files: number): string {
  return `Would delete ${String(sessions)} session(s) and ${String(files)} file(s). Nothing was deleted.`;
}
export const RUN_PURGE_DIALOG_TITLE = 'Run the purge?';
/** Names the preview's numbers, or the batch bound when there is no preview. */
export function runPurgeDialogMessage(preview: { sessions: number; files: number } | null): string {
  return preview === null
    ? 'Up to 100 expired guest sessions and their files will be deleted. This cannot be undone.'
    : `${String(preview.sessions)} expired guest session(s) and ${String(preview.files)} file(s) will be deleted. This cannot be undone.`;
}
export function purgeReportSentence(report: {
  sessions_deleted: number;
  files_unlinked: number;
  sessions_failed: number;
  files_failed: number;
  overdue: number;
}): string {
  const failures =
    report.sessions_failed + report.files_failed > 0
      ? ` ${String(report.sessions_failed)} session(s) and ${String(report.files_failed)} file(s) could not be deleted.`
      : '';
  return `Deleted ${String(report.sessions_deleted)} session(s), ${String(report.files_unlinked)} file(s).${failures} ${String(report.overdue)} still overdue.`;
}
/** 409 `purge_in_progress` — never a success sentence (AC-37). */
export const PURGE_IN_PROGRESS = 'A purge is already running. Nothing was deleted by this request.';
/** 503 `purge_failed`. */
export const PURGE_FAILED = 'The purge failed. Sessions already deleted stay deleted.';
export const PREVIEW_FAILED = "Couldn't preview. Try again.";

// The sweep (AC-38)
export const SWEEP_HEADING = 'Orphan sweep';
export const PREVIEW_SWEEP_LABEL = 'Preview sweep';
export const RUN_SWEEP_LABEL = 'Run sweep';
export function sweepPreviewSentence(report: {
  reclaimed: number;
  referenced: number;
  too_young: number;
  unrecognized: number;
}): string {
  return `Would reclaim ${String(report.reclaimed)} file(s); ${String(report.referenced)} referenced, ${String(report.too_young)} too young, ${String(report.unrecognized)} unrecognized. Nothing was deleted.`;
}
export const RUN_SWEEP_DIALOG_TITLE = 'Run the orphan sweep?';
export const RUN_SWEEP_DIALOG_MESSAGE =
  'Every unreferenced file past the age floor will be deleted. This cannot be undone.';
export function sweepReportSentence(report: { reclaimed: number; failed: number }): string {
  const failures = report.failed > 0 ? ` ${String(report.failed)} could not be removed.` : '';
  return `Reclaimed ${String(report.reclaimed)} file(s).${failures}`;
}
/** 503 `orphan_check_unavailable` (AC-38). */
export const SWEEP_CHECK_UNAVAILABLE = 'The database check could not run, so nothing was deleted.';
export const SWEEP_FAILED = "The sweep couldn't run. Nothing was deleted.";

// Job health (AC-39)
export const JOB_HEALTH_HEADING = 'Job health';
/** Keyed by `/health/ready`'s dependency names. */
export const DEPENDENCY_LABELS = {
  postgres: 'Database',
  redis: 'Redis',
  celery: 'Worker',
} as const;
export const DEPENDENCY_HEALTHY = 'Healthy';
export const DEPENDENCY_UNHEALTHY = 'Not healthy';
export const IDENTITY_SWEEP_LABEL = 'Identity token sweep';
export const NOT_REPORTED = 'Not reported by this API.';
export const READINESS_ERROR = "Couldn't read the job health.";
