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
/** AC-39: 429 `rate_limited`. The button comes back after `Retry-After` (slice 3.3: held until then). */
export const CLAIM_RATE_LIMITED = 'Too many attempts';

/** AC-14: the 429 sentence, naming the wait through `retryPhrase` (`useErrorHold(error).phrase`). */
export function claimRateLimited(retryWhen: string | null): string {
  return retryWhen === null
    ? `${CLAIM_RATE_LIMITED} — try again in a few minutes.`
    : `${CLAIM_RATE_LIMITED} — you can try again ${retryWhen}.`;
}
/** AC-39: 503, a network failure, anything unexpected. Nothing moved, so trying again is safe. */
export const CLAIM_FAILED = "Couldn't keep your work. Nothing was moved — try again.";

// ── A guest run page whose session has ended (AC-41) ────────────────────────────────────────────

/** Appended to 1.4's `guest_session_expired` copy when the visitor is signed in. */
export const SESSION_ENDED_HISTORY_NOTE =
  "If you kept this work in your account, it's in your history.";
export const SESSION_ENDED_HISTORY_LINK_LABEL = 'Open it in your history';

// ── Sentences built from counts ─────────────────────────────────────────────────────────────────

/** `1 CV`, `2 CVs` — the ordinary plural, pinned both ways by the tests. */
function countOf(count: number, singular: string, plural: string): string {
  return `${String(count)} ${count === 1 ? singular : plural}`;
}

function applications(count: number): string {
  return countOf(count, 'tailored application', 'tailored applications');
}

/** `a`, `a and b`, `a, b and c`. */
function listed(parts: readonly string[]): string {
  if (parts.length <= 1) {
    return parts.join('');
  }
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1] ?? ''}`;
}

/**
 * AC-37: the offer's first sentence — each guest CV by its `original_filename`, and the number of
 * tailored applications (left out when there are none; the summary is never empty, because
 * `summarizeGuestWork` answers `null` for nothing).
 */
export function guestWorkOfferSentence(summary: GuestWorkSummary): string {
  const parts = [...summary.cvFilenames];
  if (summary.runCount > 0) {
    parts.push(applications(summary.runCount));
  }
  return `This browser still holds work you did as a guest: ${listed(parts)}.`;
}

/**
 * AC-38: the `role="status"` note after a claim that moved something, from the **server's** counts —
 * e.g. *"Kept in your account: 1 CV, 2 tailored applications."* Only CVs and runs are named (the
 * postings and exports travel with their runs); a part that moved nothing is left out rather than
 * printed as "0". A claim that moved only postings or exports still says it kept something.
 */
export function claimSuccessNote(result: GuestWorkClaimResult): string {
  const parts: string[] = [];
  if (result.base_cvs > 0) {
    parts.push(countOf(result.base_cvs, 'CV', 'CVs'));
  }
  if (result.tailoring_runs > 0) {
    parts.push(applications(result.tailoring_runs));
  }
  return parts.length === 0
    ? 'Kept in your account.'
    : `Kept in your account: ${parts.join(', ')}.`;
}

/**
 * AC-39: a 200 that moved nothing at all ("nothing left to claim") — every count the server reports
 * as moved is zero. `working_copies_dropped` is not a move, so it does not count. The server's own
 * rule is `GuestWorkClaimReport.claimed_anything` (`domain/identity/claim.py`); keep the two in step.
 */
export function claimMovedNothing(result: GuestWorkClaimResult): boolean {
  return (
    result.base_cvs === 0 &&
    result.job_postings === 0 &&
    result.tailoring_runs === 0 &&
    result.export_jobs === 0
  );
}
