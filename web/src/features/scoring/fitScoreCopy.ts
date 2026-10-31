import { retryPhrase } from '@/features/retry/hold';

/**
 * **Every word the fit-score panel says, in one place** (slice 5.1, technical plan §6), so a test can
 * pin them: AC-47's notice verbatim, and AC-49's scan that no sentence promises a gain. The score is
 * an **estimate** of fit, never a prediction of being hired and never something to push upward.
 *
 * Sentences marked *pinned* are the spec's own words; change them only with the spec.
 */

/** The section's `h2`, rendered by the workspace that owns the section (T40). */
export const FIT_SCORE_HEADING = 'How well does your CV fit this job?';

/**
 * AC-47, pinned. Beside the button and its accessible description, so it is read *before* the click
 * that sends anything (D-5).
 */
export const FIT_SCORE_NOTICE =
  'Checking your score sends your CV text and this job posting to Google Gemini. Nothing else is sent.';

/** `not_ready`, pinned (plan §6). */
export const NOT_READY = 'Add your CV and a job posting first.';

export const LOADING = 'Loading…';

export const LOAD_ERROR = "We couldn't load your score.";

/** A read: safe to repeat, costs nothing. */
export const RETRY_LOAD = 'Retry';

/** The button, pinned (D-5). */
export const CHECK_BUTTON = 'Check my score';

/** `working`, pinned (AC-46, plan §6). */
export const WORKING = 'Checking your CV against this job… This usually takes a few seconds.';

/** Beside the number (OQ-16): *62%* *estimate*. */
export const ESTIMATE_LABEL = 'estimate';

/** `"62%"`. The number only: no colour, no adjective, no meter (AC-49). */
export function scorePercent(score: number): string {
  return `${String(score)}%`;
}

export const GAPS_HEADING = "What this job asks for that your CV doesn't show";

/** `gaps: []`, pinned (plan §6). */
export const NO_CLEAR_GAP = "We didn't find a clear gap.";

/** OQ-15, pinned: the account's sentence as corrected on 2026-10-30. */
export const ACCOUNT_RETENTION = 'Your score is saved until you delete this CV or your account.';

/** OQ-15, pinned: the guest's sentence. */
export const GUEST_RETENTION = 'We delete guest CVs and scores after 24 hours.';

/** `failed`, pinned (S-1). */
export const FAILED = "We couldn't get a score.";

export const TRY_AGAIN = 'Try again';

/** `refused`, one line per code (plan §6). No button: the same pair would be refused again. */
export const REFUSED: Readonly<Record<'scoring_refused' | 'inputs_too_large', string>> = {
  scoring_refused: "Gemini didn't return a score for this CV and job posting.",
  inputs_too_large: 'This CV is too long to score.',
};

/** `limit`, guest: OQ-9, pinned, with a link to `/register`. */
export const GUEST_LIMIT = "You've used your 3 free checks. Sign up to check more.";

export const SIGN_UP_LINK = 'Sign up';

/** `limit`, account: *"You can check again at 14:00."*, `when` from `retryPhrase` (browser locale). */
export function accountLimit(when: string): string {
  return `You can check again ${when}.`;
}

/**
 * When an account may check again, from the 409's `retry_not_before`: *"at 14:00"* today,
 * *"tomorrow at 09:00"*, or *"on Tuesday at 09:00"*, in the browser's locale. `nowMs` is when the
 * answer arrived (`useErrorHold`'s `receivedAtMs`), so the sentence is fixed text. `"later"` when
 * the instant is absent or unreadable.
 */
export function limitWhen(retryNotBefore: string | null, nowMs: number): string {
  const at = retryNotBefore === null ? Number.NaN : Date.parse(retryNotBefore);
  if (Number.isNaN(at)) {
    return 'later';
  }
  const phrase = retryPhrase(at, nowMs);
  if (!phrase.startsWith('at ')) {
    return phrase; // "now", or "in N seconds"
  }
  // The day of the minute `retryPhrase` names (it rounds up), in local calendar days.
  const named = new Date(Math.ceil(at / 60_000) * 60_000);
  const days = Math.round((startOfDay(named) - startOfDay(new Date(nowMs))) / 86_400_000);
  if (days <= 0) {
    return phrase;
  }
  if (days === 1) {
    return `tomorrow ${phrase}`;
  }
  return `on ${new Intl.DateTimeFormat(undefined, { weekday: 'long' }).format(named)} ${phrase}`;
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/**
 * `held`: a 429 still holding the button. The house hold sentence, worded as `TailorLaunch` words
 * its own (`useErrorHold`'s `phrase`, fixed at receipt).
 */
export function holdSentence(phrase: string | null): string {
  return `You can try again ${phrase ?? 'soon'}.`;
}

/** A guest's 401 `guest_session_expired`: tailoring's sentence (`rejectionMessage`), word for word. */
export const SESSION_EXPIRED = 'Your session has expired. Upload your CV again.';

/** `busy`, guest: AC-15, with a link to `/register`. */
export const GUEST_BUSY = 'Scoring is busy right now. Signing up still works.';

/** `busy`, account: AC-15. */
export const ACCOUNT_BUSY = 'Scoring is busy right now.';
