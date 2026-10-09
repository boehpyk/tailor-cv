/**
 * The words the saved-CV surfaces say (slice 2.2, AC-34…AC-46), kept out of the components that
 * say them — `authCopy.ts`'s and `exportCopy.ts`'s pattern.
 *
 * A string a test pins and a string a component happens to contain are different things, and the
 * difference shows up the day someone reflows the JSX. **Every sentence the spec quotes is here
 * verbatim**, punctuation included (some of the spec's sentences end without a full stop, and so do
 * these): AC-44, AC-45 and AC-46 are the product's privacy promise stated to the user, and a
 * paraphrase of a promise is a different promise.
 *
 * **Every refusal is chosen by `code`, never by `message`** (`api/client.ts`). The one exception is
 * deliberate and named: `too_many_saved_base_cvs` shows the server's own message, because that
 * message names the cap and the client does not know the number — re-stating it here would be the
 * business rule re-implemented in TypeScript (AC-35, Constitution §4.5).
 *
 * **Numbers in copy are descriptions, not rules.** "up to 10 MB" and "80 characters" describe
 * today's policy to a person; nothing compares against them. The API refuses, and the refusal is
 * what the user sees.
 */

import { retryPhrase } from '@/features/retry/hold';
import { ApiError } from '@/api/client';

// --- Shared ---------------------------------------------------------------------------------------

export const RETRY_LABEL = 'Retry';
export const CANCEL_LABEL = 'Cancel';

/** A thrown value that is not an `ApiError` never reached a response: offline, DNS, a dead proxy. */
export const NETWORK_FAILURE_NOTE = "Couldn't reach TailorCraft.";

/** A code this client does not know — the wire's `code` is an open set, so there is a fallback. */
export const UNKNOWN_FAILURE_NOTE = 'Something went wrong. Try again.';

/**
 * Every account call: a 401 that survived the client's one refresh-and-retry.
 * The second clause is the one that matters — signing out never deletes anything.
 */
export const SIGNED_OUT_NOTE =
  "You've been signed out — your saved CVs are safe; sign in to use them";

// --- The saved-CV section on /account (AC-34) -----------------------------------------------------

export const SAVED_CVS_HEADING = 'Saved CVs';
export const SAVED_CVS_LOADING_NOTE = 'Loading your saved CVs…';
/** AC-34 / AC-38: the list failed to load. Never the empty state — "none" and "unknown" differ. */
export const SAVED_CVS_LOAD_ERROR_NOTE = "Couldn't load your saved CVs";
export const SAVED_CVS_EMPTY_NOTE = 'No saved CVs yet';

export const RENAME_LABEL = 'Rename';
export const DELETE_LABEL = 'Delete';

/** Each `status` a row can show. `extraction_failed` also shows the server's `failure_message`. */
export const SAVED_CV_STATUS_LABELS = {
  uploaded: 'Still being read',
  extracted: 'Ready to use',
  extraction_failed: "Couldn't read this file",
} as const;

// --- Upload to the account (AC-35, AC-44) ---------------------------------------------------------

export const UPLOAD_SAVED_CV_LABEL = 'Upload a CV to your account';
/** The extraction happens inside the request, so the pending line names both halves. */
export const UPLOAD_SAVED_CV_PENDING_LABEL = 'Uploading and reading your CV…';

/** AC-44 — shown by the account upload control **before** a file is chosen. Verbatim; pinned. */
export const SAVED_CV_RETENTION_NOTICE =
  'Saved CVs stay in your account until you delete them — they are not deleted after 24 hours. We keep the file and the text we read from it. When you tailor, the AI provider sees that text.';

// --- Rename (AC-37) -------------------------------------------------------------------------------

export const RENAME_INPUT_LABEL = 'Name for this CV';
export const RENAME_SAVE_LABEL = 'Save';
export const RENAME_PENDING_LABEL = 'Saving…';

// --- Delete a saved CV (AC-36) --------------------------------------------------------------------

export const CONFIRM_DELETE_LABEL = 'Delete';
export const DELETE_PENDING_LABEL = 'Deleting…';
/** A 404 on delete: somebody (another tab) got there first. A note, not an error (AC-36). */
export const ALREADY_DELETED_NOTE = 'Already deleted';
/** A 503 (or no answer) on delete: the row stays, and the user is told it is still there. */
export const NOT_DELETED_NOTE = 'Not deleted — try again';

/**
 * AC-36's confirmation, verbatim, around the CV's display name (its label, else its filename).
 * The name is user-typed text: a component renders the return value as a text node, never as HTML
 * (AC-47).
 */
export function confirmDeleteMessage(name: string): string {
  // The last sentence is slice 2.3's (AC-45, OQ-2): deleting a saved CV leaves history intact.
  return `Delete ${name}? This removes the file and the text we read from it. This can't be undone. Working copies already in a workspace are not affected. Tailored applications you made from it stay in your history until you delete them.`;
}

// --- The workspace picker's auth gate (AC-38) -----------------------------------------------------

/** AC-38: the auth store is `unavailable` — we do not know who this is, so we cannot list anything. */
export const PICKER_UNAVAILABLE_NOTE =
  "Couldn't check your account, so saved CVs aren't available right now";

// --- The account workspace's picker, `mode: 'select'` (slice 2.3, AC-39) ---------------------------

export const SELECT_PICKER_LEGEND = 'Your base CV';
/** AC-39's empty state, beside the upload control. Verbatim; pinned. */
export const SELECT_PICKER_EMPTY_NOTE = 'Upload your CV to get started';
/**
 * The account upload control's line in the **workspace**. `SAVED_CV_RETENTION_NOTICE` is right on
 * `/account` but names "24 hours" to deny it, and account scope must not mention the guest window at
 * all (AC-49) — the workspace's own sentence already says what is kept.
 */
export const WORKSPACE_UPLOAD_NOTICE =
  'A CV you upload here is saved to your account, and stays until you delete it.';

// --- The working copy in the workspace (AC-41) ----------------------------------------------------

export const WORKING_COPY_BADGE = 'Working copy';
export const WORKING_COPY_NOTE =
  'Copy of your saved CV — deleted with this workspace in 24 hours; your saved CV stays in your account';

// --- Delete the account (AC-40, AC-46) ------------------------------------------------------------

export const DELETE_ACCOUNT_HEADING = 'Delete your account';
/**
 * AC-46, what goes: the account, the saved CVs and their files, every signed-in device — and, from
 * slice 2.3, the history (runs, postings, export files), which `EraseAccount` now erases too (T24).
 */
export const DELETE_ACCOUNT_WHAT_GOES_NOTE =
  'This deletes your account and your saved CVs, including their files and the text we read from them, and your history — every tailored CV and cover letter, job posting and exported file — and signs you out on every device.';
/** AC-46, what does not go at once. Verbatim; pinned. */
export const DELETE_ACCOUNT_WHAT_STAYS_NOTE =
  "Copies in a browser's workspace are deleted with that workspace within 24 hours";
export const DELETE_ACCOUNT_PASSWORD_LABEL = 'Your password';
export const DELETE_ACCOUNT_CONFIRM_LABEL = "I understand this can't be undone";
export const DELETE_ACCOUNT_SUBMIT_LABEL = 'Delete my account';
export const DELETE_ACCOUNT_PENDING_LABEL = 'Deleting your account…';
/** AC-40: the notice on `/` after a deletion. Verbatim. */
export const ACCOUNT_DELETED_NOTICE = 'Your account and saved CVs were deleted.';

// --- Refusals -------------------------------------------------------------------------------------

/**
 * A 401 that survived the client's one refresh-and-retry: the token is bad and a refresh could not
 * mend it, or the account behind it is gone. Either way the user is signed out now.
 */
function isSignedOut(error: ApiError): boolean {
  return error.code === 'invalid_access_token' || error.code === 'not_signed_in';
}

/** AC-35: an account upload the server refused — one distinct sentence per `code`. */
export function uploadSavedCvErrorCopy(
  error: Error,
  /** When a 429 lets the user try again, in words (`useErrorHold(error).phrase`); else `null`. */
  retryWhen: string | null,
): string {
  if (!(error instanceof ApiError)) {
    return NETWORK_FAILURE_NOTE;
  }
  if (isSignedOut(error)) {
    return SIGNED_OUT_NOTE;
  }
  switch (error.code) {
    case 'file_too_large':
      return 'That file is too large. Try a smaller file, up to 10 MB.';
    case 'unsupported_format':
      return "That file isn't a PDF, DOCX or TXT file. Try one of those.";
    case 'empty_file':
      return 'That file is empty. Choose the file with your CV in it.';
    case 'invalid_filename':
      return "That file's name can't be used. Rename the file and try again.";
    case 'missing_file':
      return 'Choose a file to upload.';
    case 'too_many_saved_base_cvs':
      // The one place the server's prose is shown: it names the cap, which the client never knows.
      return error.message;
    case 'rate_limited':
      // AC-12: the wait through `retryPhrase`, never a raw seconds count.
      return retryWhen === null
        ? 'Too many uploads in a short time.'
        : `Too many uploads in a short time. You can try again ${retryWhen}.`;
    case 'storage_unavailable':
      return "We couldn't store your file right now. Nothing was saved — try again in a moment.";
    case 'service_unavailable':
      return 'Saving CVs is unavailable right now. Nothing was saved — try again in a moment.';
    default:
      return UNKNOWN_FAILURE_NOTE;
  }
}

/** AC-37: a rename the server refused. Shown under the input, linked by `aria-describedby`. */
export function renameSavedCvErrorCopy(error: Error): string {
  if (!(error instanceof ApiError)) {
    return NETWORK_FAILURE_NOTE;
  }
  if (isSignedOut(error)) {
    return SIGNED_OUT_NOTE;
  }
  switch (error.code) {
    case 'invalid_label':
      return 'A name must be 1 to 80 characters, with no line breaks.';
    case 'validation_error':
      return 'That name is too long. Keep it to 80 characters.';
    case 'base_cv_not_found':
      return "This CV was deleted, so it can't be renamed.";
    case 'service_unavailable':
      return "Couldn't rename it right now. Try again in a moment.";
    default:
      return UNKNOWN_FAILURE_NOTE;
  }
}

/**
 * AC-36: a delete that did **not** happen. A 404 never reaches here — `useDeleteSavedBaseCv`
 * resolves it as `'already_gone'`, because "already deleted" is the outcome the user wanted.
 */
export function deleteSavedCvErrorCopy(error: Error): string {
  if (error instanceof ApiError && isSignedOut(error)) {
    return SIGNED_OUT_NOTE;
  }
  return NOT_DELETED_NOTE;
}

/**
 * AC-40: a deletion the server refused. Every sentence says the account is still there — on any
 * refusal nothing was deleted, and a user who is unsure will try again or, worse, assume it went.
 */
export function deleteAccountErrorCopy(
  error: Error,
  /** A 429's hold deadline (`useErrorHold(error).deadlineMs`), or `null`. */
  deadlineMs: number | null,
  /** When the error arrived (`useErrorHold(error).receivedAtMs`), so the wait is worded once. */
  nowMs: number,
): string {
  if (!(error instanceof ApiError)) {
    return "Couldn't reach TailorCraft. Your account was not deleted.";
  }
  if (isSignedOut(error)) {
    return "You've been signed out, so your account was not deleted. Sign in to try again.";
  }
  switch (error.code) {
    case 'password_incorrect':
      return "That password isn't right, so your account was not deleted.";
    case 'rate_limited':
      // AC-16: the wait through `retryPhrase`, never a raw seconds count.
      return deadlineMs === null
        ? 'Too many attempts. Your account was not deleted. Wait a few minutes, then try again.'
        : `Too many attempts. Your account was not deleted. You can try again ${retryPhrase(deadlineMs, nowMs)}.`;
    case 'rate_limit_unavailable':
      return 'Deleting an account is paused for a moment. Your account was not deleted — try again shortly.';
    case 'service_unavailable':
      return "We couldn't delete your account right now. Nothing was deleted — try again in a moment.";
    case 'validation_error':
      return 'Enter your password, then try again.';
    case 'origin_not_allowed':
      return 'This page is out of date. Reload TailorCraft and try again.';
    default:
      return 'Something went wrong. Your account was not deleted — try again.';
  }
}
