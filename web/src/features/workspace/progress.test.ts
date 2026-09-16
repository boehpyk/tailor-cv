import { describe, expect, it } from 'vitest';

import { makeExtractedCv, makePostingSummary } from '@/test/fixtures';

import { deriveProgress } from './progress';

import type { ProgressInput, ProgressView } from './progress';
import type { BaseCvCheck, JobPostingCheck } from '@/features/tailoring/launchReadiness';
import type { RunView } from '@/features/tailoring/runView';

/**
 * F6 RED — `deriveProgress`'s table (feature-spec AC-24, technical-plan §"Frontend").
 *
 * Written against the **spec's own row list**, not against `progress.ts`'s F5 skeleton body (which
 * unconditionally `throw new Error('not implemented')`). Every case below is expected to fail on
 * that throw until F7 — the pattern 1.3's and this slice's domain RED tasks already use: a skeleton
 * that throws is a valid "real signature, not-yet-real body", and the recorded failure is the throw
 * itself, not an `ImportError` (the module and the function both exist and are called with the real
 * `ProgressInput` shape).
 *
 * AC-24 also says "each [stage] in exactly one of pending/active/done/failed" — that is enforced by
 * `StageState` being a string-literal union rather than three booleans (`progress.ts`'s own
 * docstring makes the argument), so there is nothing further to assert about exclusivity here: a
 * stage literally cannot be two things when its type only has one slot.
 */

const MISSING_BASE_CV: BaseCvCheck = { state: 'missing' };
const MISSING_JOB_POSTING: JobPostingCheck = { state: 'missing' };
const READY_BASE_CV: BaseCvCheck = { state: 'ready', baseCv: makeExtractedCv() };
const UNREADABLE_BASE_CV: BaseCvCheck = {
  state: 'unreadable',
  baseCv: makeExtractedCv({ status: 'extraction_failed' }),
};
const READY_JOB_POSTING: JobPostingCheck = { state: 'ready', jobPosting: makePostingSummary() };

const NO_RUN: RunView = { kind: 'none' };
const RUN_QUEUED: RunView = { kind: 'working', runId: 'run-1', status: 'queued' };
const RUN_RUNNING: RunView = { kind: 'working', runId: 'run-1', status: 'running' };
const RUN_SUCCEEDED: RunView = {
  kind: 'succeeded',
  tailoredCv: { text: 'cv text', characterCount: 100 },
  coverLetter: { text: 'letter text', characterCount: 50 },
};
const RUN_FAILED_RETRYABLE: RunView = { kind: 'failed', reason: 'llm_timed_out', retryable: true };

function input(overrides: Partial<ProgressInput> = {}): ProgressInput {
  return {
    baseCv: MISSING_BASE_CV,
    isUploadingBaseCv: false,
    jobPosting: MISSING_JOB_POSTING,
    isSubmittingJobPosting: false,
    run: NO_RUN,
    ...overrides,
  };
}

describe('deriveProgress', () => {
  const cases: ReadonlyArray<{ name: string; given: ProgressInput; expect: ProgressView }> = [
    {
      name: 'nothing yet — all three stages pending',
      given: input(),
      expect: { extracting: 'pending', fetching: 'pending', tailoring: 'pending' },
    },
    {
      name: 'a base-CV upload is in flight — stage 1 active, the rest pending',
      given: input({ isUploadingBaseCv: true }),
      expect: { extracting: 'active', fetching: 'pending', tailoring: 'pending' },
    },
    {
      name: 'the base CV is extracted — stage 1 done',
      given: input({ baseCv: READY_BASE_CV }),
      expect: { extracting: 'done', fetching: 'pending', tailoring: 'pending' },
    },
    {
      name: 'the base CV failed extraction — stage 1 failed',
      given: input({ baseCv: UNREADABLE_BASE_CV }),
      expect: { extracting: 'failed', fetching: 'pending', tailoring: 'pending' },
    },
    {
      name: 'a job-posting paste or fetch is in flight — stage 2 active once the CV is done',
      given: input({ baseCv: READY_BASE_CV, isSubmittingJobPosting: true }),
      expect: { extracting: 'done', fetching: 'active', tailoring: 'pending' },
    },
    {
      name: 'a job posting is present — stage 2 done',
      given: input({ baseCv: READY_BASE_CV, jobPosting: READY_JOB_POSTING }),
      expect: { extracting: 'done', fetching: 'done', tailoring: 'pending' },
    },
    {
      name: 'the run is queued — stage 3 active',
      given: input({ baseCv: READY_BASE_CV, jobPosting: READY_JOB_POSTING, run: RUN_QUEUED }),
      expect: { extracting: 'done', fetching: 'done', tailoring: 'active' },
    },
    {
      name: 'the run is running — stage 3 active',
      given: input({ baseCv: READY_BASE_CV, jobPosting: READY_JOB_POSTING, run: RUN_RUNNING }),
      expect: { extracting: 'done', fetching: 'done', tailoring: 'active' },
    },
    {
      name: 'the run succeeded — stage 3 done',
      given: input({ baseCv: READY_BASE_CV, jobPosting: READY_JOB_POSTING, run: RUN_SUCCEEDED }),
      expect: { extracting: 'done', fetching: 'done', tailoring: 'done' },
    },
    {
      name: 'the run failed — stage 3 failed',
      given: input({
        baseCv: READY_BASE_CV,
        jobPosting: READY_JOB_POSTING,
        run: RUN_FAILED_RETRYABLE,
      }),
      expect: { extracting: 'done', fetching: 'done', tailoring: 'failed' },
    },
  ];

  it.each(cases)('$name', ({ given, expect: expected }) => {
    expect(deriveProgress(given)).toEqual(expected);
  });
});
