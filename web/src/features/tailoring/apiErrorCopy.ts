import { ApiError } from '@/api/client';

/**
 * Copy for the two ways the tailoring **API** can say no — as opposed to a run that was accepted
 * and then failed, which is `failureCopy.ts`.
 *
 * **Every sentence is chosen by `code`, never by `message`.** `message` is prose the server may
 * reword at any time, and relaying it verbatim makes the server's wording the UI's wording without
 * anyone deciding that. `code` is the contract (`client.ts`'s docstring), so it is the only thing
 * branched on here, and the copy is the failure contract's "User sees" column.
 *
 * These are `switch`es over a `string`, not an exhaustive `Record`: `code` is an open set on the
 * wire (a future endpoint change can add one), so each has a default that says something true
 * without pretending to know more.
 */

/**
 * **Error B — the API rejected `POST /api/tailoring-runs`** (G-1 … G-14).
 *
 * A thrown value that is not an `ApiError` never reached a response with an envelope: offline, DNS,
 * a proxy's HTML error page that would not parse. "We couldn't reach TailorCraft" is the honest
 * summary of all of those.
 */
export function rejectionMessage(error: Error): string {
  if (!(error instanceof ApiError)) {
    return "We couldn't reach TailorCraft. Check your connection, then try again.";
  }
  switch (error.code) {
    case 'validation_error':
      return "That request wasn't valid.";
    case 'request_too_large':
      return 'That request was too large.';
    case 'guest_session_expired':
      return 'Your session has expired. Upload your CV again.';
    case 'base_cv_not_found':
      return "We couldn't find that CV.";
    case 'job_posting_not_found':
      return "We couldn't find that job posting.";
    case 'base_cv_not_extracted':
      return "We couldn't read that CV, so there's nothing to tailor. Upload a different file.";
    case 'tailoring_already_running':
      return 'You already have a tailoring run in progress.';
    case 'too_many_tailoring_runs':
      return "You've reached the limit for this session.";
    case 'rate_limited':
      // G-11 asks for "try again in N minutes". N lives in the `Retry-After` header, which
      // `ApiError` does not carry, and the server's `message` (which may contain it) is not ours to
      // relay. "A few minutes" is true for a 10-per-hour window; a made-up number would not be.
      return 'Too many tailoring runs — try again in a few minutes.';
    case 'rate_limit_unavailable':
      return 'Tailoring is temporarily unavailable.';
    case 'queue_unavailable':
      return "We couldn't start your tailoring run. Try again.";
    case 'service_unavailable':
      return 'Something went wrong. Try again.';
    default:
      return error.status >= 500
        ? 'Something went wrong. Try again.'
        : "We couldn't start your tailoring run.";
  }
}

/**
 * The run a 409 `tailoring_already_running` says is in flight (AC-17), or `null` for any other
 * error — including that same code arriving without the id, which the UI then simply does not offer
 * to attach to.
 *
 * Read from `ApiError.details`, the envelope's remaining keys. Narrowed with `typeof` here, at the
 * one place that knows what this key means, rather than trusted as a shape.
 */
export function activeTailoringRunId(error: Error): string | null {
  if (!(error instanceof ApiError) || error.code !== 'tailoring_already_running') {
    return null;
  }
  const id = error.details['active_tailoring_run_id'];
  return typeof id === 'string' ? id : null;
}

/** What the panel says when it can no longer read the run it was watching. */
export interface RunReadErrorCopy {
  readonly message: string;
  /**
   * Whether asking again could plausibly give a different answer. `false` for a 4xx — the run is
   * gone or the session is — so the panel offers a fresh start instead; `true` for a transient
   * failure, where the run may well still be working and a free re-read is the right offer.
   */
  readonly canCheckAgain: boolean;
}

/**
 * `GET /api/tailoring-runs/{id}` failing while a run is watched — the poller has stopped (see
 * `useTailoringRun`), and the user must be told whether the run is gone or merely out of sight.
 *
 * The last case matters most: after a network failure the run may be **still working**, so the copy
 * says so and offers "Check again" — a free read — rather than anything that reads like "this
 * failed", which is what sends a user to pay for a second run.
 */
export function runReadErrorCopy(error: Error): RunReadErrorCopy {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    switch (error.code) {
      case 'tailoring_run_not_found':
        return { message: "We couldn't find that run.", canCheckAgain: false };
      case 'guest_session_expired':
        return { message: 'Your session has expired. Upload your CV again.', canCheckAgain: false };
      default:
        return { message: "We couldn't load that run.", canCheckAgain: false };
    }
  }
  return {
    message: 'We lost contact with your tailoring run. It may still be working.',
    canCheckAgain: true,
  };
}
