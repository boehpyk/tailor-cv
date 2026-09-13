import type { BaseCv } from '@/features/intake/types';
import type { JobPostingSummary } from '@/features/posting/types';

import type { NewTailoringRun } from './types';

/**
 * Whether the two things a run needs are on screen and usable — the whole input to **error A**,
 * "rejected before the request".
 *
 * This is UX pre-validation, not a business rule (Constitution §4.5). The API re-checks both on
 * every `POST` — a 404 for a CV or posting that is not the session's, a 409
 * `base_cv_not_extracted` for a CV with no text — and is the only authority. The job here is
 * narrower: an obviously doomed click should not issue a request at all, and the user should be
 * told *why* the button is disabled rather than left to guess.
 *
 * Each check is a discriminated union rather than a boolean, because the checklist has to say more
 * than "not ready": a CV still being read and a CV that could not be read are two different things
 * to do next.
 */
export type BaseCvCheck =
  | { readonly state: 'ready'; readonly baseCv: BaseCv }
  | { readonly state: 'reading'; readonly baseCv: BaseCv }
  | { readonly state: 'unreadable'; readonly baseCv: BaseCv }
  | { readonly state: 'missing' };

export type JobPostingCheck =
  | { readonly state: 'ready'; readonly jobPosting: JobPostingSummary }
  | { readonly state: 'missing' };

/**
 * `baseCv` must be the CV `BaseCvUploadPanel` is showing — pass `latestBaseCv(items)`, never pick
 * one another way, or this panel would tailor a different CV from the one on screen.
 *
 * A `switch` over the status union, so a fourth status is a compile error here rather than a CV
 * the checklist silently calls ready.
 */
export function checkBaseCv(baseCv: BaseCv | null): BaseCvCheck {
  if (baseCv === null) {
    return { state: 'missing' };
  }
  switch (baseCv.status) {
    case 'extracted':
      return { state: 'ready', baseCv };
    case 'uploaded':
      return { state: 'reading', baseCv };
    case 'extraction_failed':
      return { state: 'unreadable', baseCv };
  }
}

/** `jobPosting` must be `latestPosting(items)`, for the reason `checkBaseCv` gives. */
export function checkJobPosting(jobPosting: JobPostingSummary | null): JobPostingCheck {
  return jobPosting === null ? { state: 'missing' } : { state: 'ready', jobPosting };
}

/**
 * The request body, or `null` when either check is not `ready`. Returning `null` rather than a
 * body-plus-flag is what makes "no request without both inputs" true by construction: there is no
 * `NewTailoringRun` to send until the narrowing below has found both ids.
 */
export function launchInput(
  baseCv: BaseCvCheck,
  jobPosting: JobPostingCheck,
): NewTailoringRun | null {
  if (baseCv.state !== 'ready' || jobPosting.state !== 'ready') {
    return null;
  }
  return { base_cv_id: baseCv.baseCv.id, job_posting_id: jobPosting.jobPosting.id };
}
