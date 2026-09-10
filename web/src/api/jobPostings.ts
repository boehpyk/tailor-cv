import { request } from './client';

import type { JobPosting, JobPostingListResponse, NewJobPosting } from '@/features/posting/types';

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
export function fetchJobPostings(signal?: AbortSignal): Promise<JobPostingListResponse> {
  return request<JobPostingListResponse>('/api/job-postings', {
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
export function createJobPosting(input: NewJobPosting, signal?: AbortSignal): Promise<JobPosting> {
  return request<JobPosting>('/api/job-postings', {
    method: 'POST',
    body: input,
    ...(signal ? { signal } : {}),
  });
}
