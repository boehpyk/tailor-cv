/* eslint-disable @typescript-eslint/no-unused-vars -- T29 SKELETON: the parameters are the signature qa's T30 tests compile against; T31 uses them and deletes this line. */
import type { GuestWorkSummary } from './guestWorkSummary';
import type { GuestWorkClaimResult } from './types';

/**
 * Every sentence slice 2.4 puts on screen (technical plan §7), in one module so a reworded line
 * changes in one place and a test can import the string it pins instead of retyping it.
 *
 * The constants are the spec's words verbatim (AC-35, AC-37, AC-38, AC-39, AC-41). The two
 * functions build a sentence from counts.
 */

// ── The registration CTA (AC-35) ────────────────────────────────────────────────────────────────

/** The CTA's `role="region"` label. */
export const CTA_REGION_LABEL = 'Save your work';
/** PRD §6, verbatim: the soft registration prompt after the first generation. */
export const CTA_SENTENCE = 'Save your base CV and tailoring history for future applications.';
/** The retention line — true here, because the CTA renders only on a guest's run. */
export const CTA_RETENTION_LINE = 'Your work here is deleted within 24 hours.';
export const CTA_REGISTER_LABEL = 'Create an account';
export const CTA_LOGIN_LABEL = 'Sign in';
/** Shared by the CTA and the offer: hides it for this page's lifetime, sends nothing (AC-40). */
export const NOT_NOW_LABEL = 'Not now';

// ── The claim offer (AC-37…AC-40) ───────────────────────────────────────────────────────────────

/** The offer's `role="region"` label (AC-45). */
export const OFFER_REGION_LABEL = 'Keep your work';
/** AC-37, verbatim: what keeping means, and what not keeping means. */
export const OFFER_RETENTION_LINE =
  "Keep them in your account and they'll be saved until you delete them. Otherwise they're deleted within 24 hours.";
export const KEEP_LABEL = 'Keep them in my account';
/** AC-38: the button's label while the claim is in flight (the button is disabled meanwhile). */
export const KEEP_PENDING_LABEL = 'Keeping your work…';

/** AC-39: 200 with every count zero — a success that moved nothing; the offer goes. */
export const CLAIM_NOTHING_LEFT =
  "There was nothing left to keep — this browser's guest work has already been kept or has expired.";
/** AC-39: 429 `rate_limited`. The button comes back after `Retry-After`. */
export const CLAIM_RATE_LIMITED = 'Too many attempts — try again in a few minutes.';
/** AC-39: 503, a network failure, anything unexpected. Nothing moved, so trying again is safe. */
export const CLAIM_FAILED = "Couldn't keep your work. Nothing was moved — try again.";

// ── A guest run page whose session has ended (AC-41) ────────────────────────────────────────────

/** Appended to 1.4's `guest_session_expired` copy when the visitor is signed in. */
export const SESSION_ENDED_HISTORY_NOTE =
  "If you kept this work in your account, it's in your history.";
export const SESSION_ENDED_HISTORY_LINK_LABEL = 'Open it in your history';

// ── Sentences built from counts ─────────────────────────────────────────────────────────────────

/**
 * AC-37: the offer's first sentence — each guest CV by its `original_filename`, and the number of
 * tailored applications.
 *
 * SKELETON (T29): returns an empty string; T31 writes the sentence.
 */
export function guestWorkOfferSentence(_summary: GuestWorkSummary): string {
  return '';
}

/**
 * AC-38: the `role="status"` note after a claim that moved something, from the **server's** counts —
 * e.g. *"Kept in your account: 1 CV, 2 tailored applications."*
 *
 * SKELETON (T29): returns an empty string; T31 writes the sentence.
 */
export function claimSuccessNote(_result: GuestWorkClaimResult): string {
  return '';
}
