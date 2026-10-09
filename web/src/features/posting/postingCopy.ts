import { ApiError } from '@/api/client';

/**
 * **Error B — the API refused `POST` of a job posting** (AC-13). The words that can carry a number
 * the server chose — a 429's wait, a 5xx — are chosen by `code` here, so the server's prose
 * (*"Try again in 120 seconds"*) never reaches the DOM and the wait reads through `retryPhrase`.
 *
 * A refusal **about the text itself** (`posting_text_too_short`, `…_too_long`, a validation error)
 * still shows the server's sentence: it names the limit the server enforces, which the client must
 * not restate (the same call as `too_many_saved_base_cvs`).
 */
export function postingRejectionMessage(
  error: Error,
  /** When a 429 lets the user try again, in words (`useErrorHold(error).phrase`); else `null`. */
  retryWhen: string | null,
): string {
  if (!(error instanceof ApiError)) {
    return "We couldn't reach TailorCraft. Check your connection, then try again.";
  }
  switch (error.code) {
    case 'rate_limited':
      return retryWhen === null
        ? 'Too many job postings in a short time.'
        : `Too many job postings in a short time. You can try again ${retryWhen}.`;
    case 'rate_limit_unavailable':
      return 'Adding job postings is unavailable right now. Try again shortly.';
    case 'guest_session_expired':
      return 'Your session has expired. Upload your CV again.';
    default:
      return error.status >= 500 || error.code === null
        ? 'Something went wrong. Try again.'
        : error.message;
  }
}
