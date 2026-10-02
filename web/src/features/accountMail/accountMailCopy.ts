/* eslint-disable @typescript-eslint/no-unused-vars -- T36 SKELETON: the parameters are the signature qa's T37 tests compile against; T38 uses them and deletes this line. */
/**
 * The words the mail-link screens say (slice 2.5, AC-45 … AC-49) — `authCopy.ts`'s pattern: every
 * sentence the spec states verbatim lives here, so a test pins a constant rather than whatever the
 * JSX happens to contain.
 *
 * **Every sentence is the same for every address.** Nothing here may say "new account" or "no such
 * account": the server answers 202 either way on purpose (ADR-0008 (h)), and copy that guessed
 * would undo it.
 *
 * SKELETON (T36): the static sentences are final; the two built from an address return `''` until
 * T38.
 */

// --- The mail provider (OQ-8) ---------------------------------------------------------------------

/**
 * The name of the service that sends our mail, as the user reads it. **Product copy, not config**:
 * it is not fetched from the server and not an env var.
 *
 * TODO(T44): a placeholder until the owner names the mail vendor (OQ-8). Replace with the
 * provider's name before release.
 */
export const MAIL_PROVIDER_NAME = 'our email provider';

/** The disclosure on every screen that promises a mail (plan §7). */
export const MAIL_PROVIDER_SENTENCE = `We'll email you through ${MAIL_PROVIDER_NAME}, which handles the message and sees your email address.`;

// --- Shared ---------------------------------------------------------------------------------------

/** The pending label of every form that sends a mail (register, *Send it again*, reset request). */
export const SENDING_LABEL = 'Sending…';
export const LOG_IN_LABEL = 'Log in';
export const CREATE_ACCOUNT_LABEL = 'Create an account';
export const RESET_PASSWORD_LINK_LABEL = 'Reset your password';

// --- Check your email (AC-45) ---------------------------------------------------------------------

export const CHECK_EMAIL_HEADING = 'Check your email';

/** *"Check your inbox at {email}. …"* — the same for every address. SKELETON: `''`. */
export function checkInboxSentence(_email: string): string {
  return '';
}

export const SEND_AGAIN_LABEL = 'Send it again';
export const SENT_AGAIN_NOTE = 'Sent again.';
export const SEND_AGAIN_FAILED = "Couldn't send just now — try again.";
export const USE_DIFFERENT_EMAIL_LABEL = 'Use a different email';
export const ALREADY_CONFIRMED_PROMPT = 'Already confirmed?';
/** Shown only in the guest scope when this browser has guest work (AC-45). */
export const GUEST_WORK_DEADLINE_NOTE =
  'Your work here is kept for 24 hours — confirm and log in before then to keep it.';

// --- /confirm-email (AC-46) -----------------------------------------------------------------------

export const CONFIRM_EMAIL_HEADING = 'Confirm your email address';
export const CONFIRM_EMAIL_LABEL = 'Confirm my email address';
export const CONFIRMING_LABEL = 'Confirming…';
export const EMAIL_CONFIRMED_NOTE = 'Your email address is confirmed.';
export const CONFIRM_LINK_INVALID =
  "This link has expired or has already been used. If you've already confirmed, log in. Otherwise, create your account again.";
export const CONFIRM_ALREADY_REGISTERED =
  'This address already has an account. Log in, or reset your password.';
export const CONFIRM_UNAVAILABLE = "Couldn't confirm just now. Nothing changed — try again.";

/** The empty state of both landing pages: no token in the fragment (AC-46, AC-48, V-62, V-63). */
export const LINK_INCOMPLETE = 'This link is incomplete. Open the link from the email again.';

// --- /reset-password (AC-47) ----------------------------------------------------------------------

export const RESET_REQUEST_HEADING = 'Reset your password';
export const RESET_REQUEST_SUBMIT_LABEL = 'Send reset link';

/** *"If there's an account for {email}, …"* — neutral on purpose. SKELETON: `''`. */
export function resetRequestedSentence(_email: string): string {
  return '';
}

// --- /reset-password/confirm (AC-48) --------------------------------------------------------------

export const RESET_CONFIRM_HEADING = 'Choose a new password';
export const RESET_CONFIRM_SUBMIT_LABEL = 'Save new password';
export const RESET_CONFIRM_PENDING_LABEL = 'Saving your new password…';
export const RESET_LINK_INVALID = 'This reset link has expired or has already been used.';
export const SEND_NEW_LINK_LABEL = 'Send a new link';
export const PASSWORD_CHANGED_NOTE =
  "Your password has been changed and you've been logged out everywhere.";

// --- /login (AC-49) -------------------------------------------------------------------------------

export const FORGOT_PASSWORD_LABEL = 'Forgot your password?';
/** `invalid_credentials`' second sentence — static, the same for every failure. */
export const CONFIRM_EMAIL_FIRST_NOTE =
  'Just created an account? Confirm your email address first — check your inbox.';
