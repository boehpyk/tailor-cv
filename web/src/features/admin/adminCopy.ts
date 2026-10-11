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
