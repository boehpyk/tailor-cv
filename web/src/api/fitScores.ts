import { request } from './client';
import { authOptionFor } from './target';

import type { FitScore, FitScoreForPair, FitScorePair } from '@/features/scoring/types';
import type { ApiTarget } from './target';

/**
 * The fit-score endpoints (slice 5.1, technical plan §4). Both serve both scopes through an
 * `ApiTarget`: `/api/fit-scores` with the guest cookie, `/api/me/fit-scores` with the bearer. The
 * bodies and answers are the same; only `expires_at` (`null` for an account) and a limit's
 * `retry_not_before` differ.
 *
 * **There is no by-id route.** The client asks about the pair, which is all it knows.
 */

/**
 * Score a pair. **This is the only call that reaches Google Gemini**, and it is made on a click,
 * never on render (AC-48). **201** a new score, **200** a stored one (a hit, or a race's loser):
 * the same shape, so the caller doesn't care which.
 *
 * Rejections are `ApiError`s keyed by `code`: 401 `guest_session_expired` / `invalid_access_token`;
 * 403 `account_disabled`; 404 `base_cv_not_found` / `job_posting_not_found`; 409
 * `base_cv_not_extracted` or `too_many_fit_scores` (`details.limit`, `details.retry_not_before`,
 * `null` for a guest); 413; 422 `validation_error` / `scoring_refused` / `inputs_too_large`; 429
 * `rate_limited` with `Retry-After`; 503 `scoring_busy` / `scoring_failed` /
 * `rate_limit_unavailable` / `service_unavailable`.
 *
 * Never retried automatically: a retried `POST` is a second paid call (`retry.grep.test.ts`).
 */
export function requestFitScore(
  target: ApiTarget,
  pair: FitScorePair,
  signal?: AbortSignal,
): Promise<FitScore> {
  return request<FitScore>(target.fitScoresPath, {
    method: 'POST',
    body: { base_cv_id: pair.base_cv_id, job_posting_id: pair.job_posting_id },
    ...authOptionFor(target),
    ...(signal ? { signal } : {}),
  });
}

/**
 * The stored score for a pair, or `{ fit_score: null }`. Reads only: no `GET` reaches the scorer.
 * Rejections: 401, 403 (account), 404 (either id), 422, 503 `service_unavailable`.
 */
export function fetchFitScoreForPair(
  target: ApiTarget,
  pair: FitScorePair,
  signal?: AbortSignal,
): Promise<FitScoreForPair> {
  const query = new URLSearchParams({
    base_cv_id: pair.base_cv_id,
    job_posting_id: pair.job_posting_id,
  });
  return request<FitScoreForPair>(`${target.fitScoresPath}?${query.toString()}`, {
    ...authOptionFor(target),
    ...(signal ? { signal } : {}),
  });
}
