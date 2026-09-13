import type { TailoringFailureReason } from './types';

/** What error C says: the sentence, and an optional second line with the thing to do about it. */
export interface FailureCopy {
  readonly headline: string;
  readonly hint: string | null;
}

/**
 * `failure_reason` → copy, for **error C — the run itself failed** (the failure contract's "User
 * sees" column, G-16 … G-25).
 *
 * **Exhaustive by its type.** `Record<TailoringFailureReason, …>` requires one entry per member of
 * the union, so a tenth reason added to `types.ts` is a compile error on this line — the one place
 * that must write a sentence for it — instead of a blank amber box in a user's browser.
 *
 * **What this map does not decide is whether "Try again" is offered.** That is the run's
 * `retryable`, computed by the API from the same reason (AC-13). Encoding "`llm_refused` is not
 * worth retrying" here as well would be a second copy of a business rule, and the copy is the one
 * that goes stale. So the hints below never say "try again" for a reason that is not retryable —
 * but they do not decide it either.
 *
 * `not_queued` and `abandoned` have no row of their own in the "User sees" column for a *polled*
 * run (G-14's copy is error B's, for the 503), so their sentences are written to say what happened
 * without repeating B's wording: the same event should not read as the same incident twice.
 */
const FAILURE_COPY: Readonly<Record<TailoringFailureReason, FailureCopy>> = {
  llm_unavailable: { headline: "We couldn't reach the model.", hint: null },
  llm_rate_limited: {
    headline: 'The model is busy right now.',
    // PRD §6's backoff hint. Deliberately vague about the number: the provider's own retry-after
    // is not in the response, and a precise figure we made up would be a promise we cannot keep.
    hint: 'Give it a minute before you try again.',
  },
  llm_refused: { headline: 'The model declined to rewrite this content.', hint: null },
  llm_timed_out: { headline: 'That took too long.', hint: null },
  llm_output_invalid: { headline: "The model's answer wasn't usable.", hint: null },
  inputs_too_large: {
    headline: 'Your CV is longer than we can tailor in one go — trim it and upload again.',
    hint: null,
  },
  llm_error: { headline: 'Something went wrong generating your documents.', hint: null },
  not_queued: { headline: 'That run never reached a worker.', hint: null },
  abandoned: { headline: 'That run was interrupted.', hint: null },
};

/**
 * A `failed` run's `failure_reason` is typed nullable because the wire type is shared with runs
 * that have not failed. The domain never records a failure without a reason, but the type allows
 * it, so the branch is handled rather than asserted away.
 */
const UNKNOWN_FAILURE_COPY: FailureCopy = {
  headline: 'Something went wrong generating your documents.',
  hint: null,
};

export function failureCopyFor(reason: TailoringFailureReason | null): FailureCopy {
  return reason === null ? UNKNOWN_FAILURE_COPY : FAILURE_COPY[reason];
}
