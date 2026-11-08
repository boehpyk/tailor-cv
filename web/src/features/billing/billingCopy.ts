import type { CreditEntryKind, CreditNotice } from './types';

/**
 * **Every word the credit surfaces say, in one place** (slice 5.2, technical plan §5), so a test can
 * pin them and AC-37's scan can read them: a credit is never called a *token*, and nothing here
 * promises a gain. A pack is **not sold** in this slice: every sentence about one says payments are
 * opening soon, and that nothing was charged.
 *
 * Sentences marked *pinned* are the spec's own words; change them only with the spec.
 */

// --- The line beside Tailor (AC-33) ---------------------------------------------------------------

/** Loading: Tailor stays enabled (the server decides). */
export const CREDITS_LOADING = 'Checking your credits…';

/** Error: Tailor stays enabled; a 409 `no_credits` is what would show the out-of-credits panel. */
export const CREDITS_LOAD_ERROR = "Couldn't load your credits.";

/** *"Uses 1 credit · You have 3"*, pinned (AC-33). */
export function creditsLine(balance: number): string {
  return `Uses 1 credit · You have ${String(balance)}`;
}

// --- The one-time notice (AC-34) ------------------------------------------------------------------

/** One per `notice`. The existing-account variant says everything made before stays free. */
export const CREDITS_NOTICE: Readonly<Record<CreditNotice, string>> = {
  credits_introduced:
    'Tailoring now uses credits: one tailored CV and cover letter is one credit. Your account has 1 free credit, and everything you made before stays free to open, edit and download.',
  first_credit:
    'Your first tailoring is free: your account starts with 1 credit. One tailored CV and cover letter uses one credit.',
};

// --- Out of credits (AC-33) -----------------------------------------------------------------------

export const OUT_OF_CREDITS_HEADING = "You're out of credits";

/** Pinned (AC-33). */
export const OUT_OF_CREDITS_KEEP =
  "Everything you've made stays here: open, edit and download it any time.";

/** Under a failed account run, pinned (AC-33). */
export const FAILED_RUN_REFUND = "Failed runs don't use a credit.";

// --- Packs (AC-35, D-6) ---------------------------------------------------------------------------

export const PACKS_HEADING = 'Credit packs';

/** `500` → `"$5"`, `1250` → `"$12.50"`. US dollars, the server's price in cents. */
export function priceLabel(priceCents: number): string {
  const whole = priceCents % 100 === 0;
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: whole ? 0 : 2,
    maximumFractionDigits: whole ? 0 : 2,
  }).format(priceCents / 100);
}

/** A pack button: *"3 credits for $5"*. */
export function packLabel(credits: number, priceCents: number): string {
  return `${String(credits)} credits for ${priceLabel(priceCents)}`;
}

/** While a pack click is in flight, on its button only. */
export const PACK_PENDING = 'One moment…';

// --- The pack dialog (AC-35) ----------------------------------------------------------------------

export const PACK_DIALOG_HEADING = 'Payments are opening soon';

/** Success: payments aren't open, and nothing was charged. */
export const PACK_OPENING_SOON =
  "We're not taking payments yet, so nothing was charged. Thanks for telling us which pack you'd choose.";

/** Pinned (plan §5). */
export const PACK_CLICK_ERROR = "That didn't go through. Nothing was charged.";

export const PACK_TRY_AGAIN = 'Try again';

/** The opt-in names the address and promises one plain-text email and nothing else (AC-35). */
export function optInLabel(email: string): string {
  return `Email me once at ${email} when payments open. One plain-text email, nothing else.`;
}

export const OPT_IN_SAVING = 'Saving…';

export const OPT_IN_ERROR = "Couldn't save that. Try again.";

export const CLOSE = 'Close';

// --- The account page (plan §5) -------------------------------------------------------------------

export const CREDITS_HEADING = 'Credits';

/** *"You have 1 credit"* / *"You have 3 credits"*. */
export function balanceLine(balance: number): string {
  return `You have ${String(balance)} ${balance === 1 ? 'credit' : 'credits'}`;
}

export const ENTRIES_HEADING = 'Credit activity';

/** Pinned (plan §5). */
export const NO_ENTRIES = 'No credit activity yet.';

export const RETRY = 'Retry';

/** One label per ledger line. */
export const ENTRY_LABELS: Readonly<Record<CreditEntryKind, string>> = {
  welcome: 'Free credit for your new account',
  existing_account: 'Free credit for your account',
  grant: 'Credits added',
  charge: 'Tailoring',
  refund: 'Returned for a failed run',
};

/** `+1`, `−1` (a real minus sign). */
export function signedCredits(credits: number): string {
  return credits < 0 ? `−${String(-credits)}` : `+${String(credits)}`;
}

// --- Erasure (AC-32) ------------------------------------------------------------------------------

/** *"2 unused credits will be removed and can't be restored."*, shown when N > 0 (AC-32). */
export function unusedCreditsWarning(n: number): string {
  return `${String(n)} unused ${n === 1 ? 'credit' : 'credits'} will be removed and can't be restored.`;
}

/** Pinned (AC-32): the balance could not be read. Erasure is never blocked by billing. */
export const UNUSED_CREDITS_FALLBACK = 'Any unused credits are removed too.';

// --- The guest offer (AC-36) ----------------------------------------------------------------------

export const GUEST_OFFER_HEADING = 'Want this CV tailored to the job?';

/** Below the guest's fit score. */
export const GUEST_OFFER_WORKSPACE =
  'Create a free account and your first tailoring is free: a CV and cover letter written for this job, ready to edit and download.';

/** On a guest run page, where the export bar was. */
export const GUEST_OFFER_RUN =
  'Downloads now need an account. Create a free account and your first tailoring is free.';

export const GUEST_OFFER_SIGN_UP = 'Create free account';

export const GUEST_OFFER_SIGN_IN = 'Sign in';
