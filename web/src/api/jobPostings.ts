import { request } from './client';
import { ACCOUNT_API_TARGET, authOptionFor } from './target';

import type { JobPosting, JobPostingListResponse, NewJobPosting } from '@/features/posting/types';
import type { ApiTarget } from './target';

/**
 * The job-posting endpoints. No change to `client.ts` was needed: the JSON path already works, and
 * the `FormData` branch slice 1.1 added for uploads is untouched.
 */

/**
 * Every job posting the caller's guest session owns, as summaries with a preview. `items` is `[]`
 * for a session with none — never a 404.
 *
 * A missing, unknown or expired `tc_guest` cookie is a **401 `guest_session_expired`**, thrown as
 * an `ApiError` like any other failure; this function does not special-case it. What that 401
 * *means* for the UI is a rendering decision, not a transport one, so it is made in
 * `useJobPostings`, one layer up.
 */
export function fetchJobPostings(
  target: ApiTarget,
  signal?: AbortSignal,
): Promise<JobPostingListResponse> {
  // For an account this is `GET /api/me/job-postings?limit=1` — the newest posting, which is
  // exactly what the workspace reads from the guest's full list too (AC-40).
  return request<JobPostingListResponse>(target.latestJobPostingsPath, {
    ...authOptionFor(target),
    ...(signal ? { signal } : {}),
  });
}

/** One job posting in full, including its text. */
export function fetchJobPosting(id: string, signal?: AbortSignal): Promise<JobPosting> {
  return request<JobPosting>(`/api/job-postings/${encodeURIComponent(id)}`, {
    ...(signal ? { signal } : {}),
  });
}

/**
 * Capture one job posting — pasted text or a link to fetch.
 *
 * One endpoint for both, with the source in the body (ADR-0013). `input` is the tagged union from
 * `features/posting/types.ts`, so a body carrying both `text` and `url`, or neither, does not
 * compile here rather than being refused at the boundary.
 *
 * Unlike the two reads, the API tolerates a missing or expired cookie by minting a fresh guest
 * session rather than answering 401, so this call never needs the session-expiry handling
 * `fetchJobPostings` defers to its caller.
 */
export function createJobPosting(
  target: ApiTarget,
  input: NewJobPosting,
  signal?: AbortSignal,
): Promise<JobPosting> {
  return request<JobPosting>(target.jobPostingsPath, {
    method: 'POST',
    body: input,
    ...authOptionFor(target),
    ...(signal ? { signal } : {}),
  });
}

/**
 * The account's twins of the two calls above (slice 2.3, technical plan §4): the same bodies and
 * the same response shapes, under `/api/me/job-postings`, **`auth: 'required'`** and nothing else
 * (AC-40, AC-48). A posting captured here is born user-owned — kept until the user deletes the
 * history entry that uses it — so its `expires_at` is `null`.
 *
 * Named twins beside the target-taking functions above, for a caller that is account-only by
 * construction (a picker of recent postings) and holds no scope: the path and the credential still
 * come from one value, `ACCOUNT_API_TARGET`, never from a boolean threaded by a caller.
 */

/**
 * The account's most recent postings, newest first — `limit` of them (1…20; the server's default is
 * 1, the workspace's posting card). A picker, not an archive: there is no cursor.
 *
 * Refusals: 401 `invalid_access_token` / `not_signed_in`; 422 `validation_error`; 503
 * `service_unavailable`.
 */
export function fetchRecentAccountJobPostings(
  limit: number,
  signal?: AbortSignal,
): Promise<JobPostingListResponse> {
  return request<JobPostingListResponse>(
    `${ACCOUNT_API_TARGET.jobPostingsPath}?limit=${encodeURIComponent(String(limit))}`,
    {
      ...authOptionFor(ACCOUNT_API_TARGET),
      ...(signal ? { signal } : {}),
    },
  );
}

/**
 * Capture one job posting into the account — pasted text or a link to fetch, 1.2's tagged union.
 * **201** `JobPosting` with `expires_at: null`. Unlike the guest `POST`, nothing is minted: a
 * missing or stale bearer is a 401, answered once by the client's refresh-and-retry.
 *
 * Refusals: 401 ×2; 409 `too_many_job_postings`; 413 `request_too_large`; 422 1.2's codes (the
 * `FETCH_FAILURE_CODES` among them, which the UI answers with the paste fallback); 429
 * `rate_limited`; 503 `rate_limit_unavailable` / `service_unavailable`.
 */
export function createAccountJobPosting(
  input: NewJobPosting,
  signal?: AbortSignal,
): Promise<JobPosting> {
  return createJobPosting(ACCOUNT_API_TARGET, input, signal);
}
