import { ApiError } from '../../api/client';
import type { FitScore, FitScoreForPair } from './types';

/**
 * **What the fit-score panel shows**, as one discriminated union (slice 5.1, technical plan §6,
 * AC-45). Pure: the panel's inputs in, one view out, so every state and its precedence is a truth
 * table (T36) rather than a tangle of booleans in JSX that can render *working* and *failed* at once.
 *
 * | kind | when |
 * |---|---|
 * | `not_ready` | no pair: a CV or a posting is missing, or the CV isn't readable yet |
 * | `loading` | the pair's `GET` has no answer yet |
 * | `load_error` | the `GET` failed after its retries |
 * | `empty` | `fit_score: null`, no request error: the notice and *Check my score* |
 * | `working` | the `POST` is in flight |
 * | `succeeded` | a stored score: the number, *estimate*, the gaps |
 * | `failed` | 503 `scoring_failed` / `service_unavailable` / `rate_limit_unavailable`, or no response |
 * | `refused` | 422 `scoring_refused` / `inputs_too_large`: no button |
 * | `limit` | 409 `too_many_fit_scores`; `retryNotBefore` is `null` for a guest |
 * | `busy` | 503 `scoring_busy`: the day's cap |
 * | `held` | a 429 still holding (`useErrorHold`) |
 */
export type FitScoreView =
  | { readonly kind: 'not_ready' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'load_error' }
  | { readonly kind: 'empty' }
  | { readonly kind: 'working' }
  | { readonly kind: 'succeeded'; readonly score: FitScore }
  | { readonly kind: 'failed' }
  | { readonly kind: 'refused'; readonly code: 'scoring_refused' | 'inputs_too_large' }
  | { readonly kind: 'limit'; readonly retryNotBefore: string | null }
  | { readonly kind: 'busy' }
  | { readonly kind: 'held' };

/** The views a request's failure produces: what `FitScoreFailure` renders. */
export type FitScoreFailureView = Extract<
  FitScoreView,
  { readonly kind: 'failed' | 'refused' | 'limit' | 'busy' | 'held' }
>;

/**
 * Everything the view depends on, read from the two hooks and the hold. Plain fields, not the
 * TanStack results, so a test builds one by hand.
 */
export interface FitScoreViewInput {
  /** Whether both a usable CV and a posting are on screen (`launchInput(...) !== null`). */
  readonly hasPair: boolean;
  /** The `GET`: `useFitScore`'s `status` and `data` (TanStack keeps `data` after a failed refetch). */
  readonly read: {
    readonly status: 'pending' | 'error' | 'success';
    readonly data: FitScoreForPair | undefined;
  };
  /** The `POST`: `useRequestFitScore`'s `isPending` and `error` (an `ApiError`, or not, for a network failure). */
  readonly request: {
    readonly isPending: boolean;
    readonly error: Error | null;
  };
  /** `useErrorHold(request.error).held`: a 429 whose `Retry-After` hasn't passed. */
  readonly held: boolean;
}

/**
 * Precedence, highest first (technical plan, "Precedence corrected 2026-11-01"): `not_ready`;
 * `working`; a stored score (even after a failed refetch, or a failed `POST`: AC-46); `held`; the
 * request's outcome; `loading` / `load_error`; `empty`.
 */
export function fitScoreView(input: FitScoreViewInput): FitScoreView {
  if (!input.hasPair) return { kind: 'not_ready' };
  if (input.request.isPending) return { kind: 'working' };
  const stored = input.read.data?.fit_score ?? null;
  if (stored !== null) return { kind: 'succeeded', score: stored };
  if (input.held) return { kind: 'held' };
  const outcome = requestOutcome(input.request.error);
  if (outcome !== null) return outcome;
  if (input.read.data !== undefined) return { kind: 'empty' };
  switch (input.read.status) {
    case 'error':
      return { kind: 'load_error' };
    // TanStack v5 never resolves `success` with `undefined`; treat it as not answered yet.
    case 'pending':
    case 'success':
      return { kind: 'loading' };
  }
}

/**
 * What a finished `POST`'s error shows, or `null` for none: no error, or a 429 whose hold has
 * expired (`held` is checked before this). A missing pair id means the pair is gone: `not_ready`.
 */
function requestOutcome(error: Error | null): FitScoreView | null {
  if (error === null) return null;
  if (!(error instanceof ApiError)) return { kind: 'failed' };
  switch (error.code) {
    case 'scoring_refused':
    case 'inputs_too_large':
      return { kind: 'refused', code: error.code };
    case 'too_many_fit_scores': {
      const at = error.details.retry_not_before;
      return { kind: 'limit', retryNotBefore: typeof at === 'string' ? at : null };
    }
    case 'scoring_busy':
      return { kind: 'busy' };
    case 'base_cv_not_found':
    case 'job_posting_not_found':
    case 'base_cv_not_extracted':
      return { kind: 'not_ready' };
    default:
      return error.status === 429 ? null : { kind: 'failed' };
  }
}
