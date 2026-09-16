import { describe, expect, it } from 'vitest';

import { ApiError } from '@/api/client';
import { makeRun, makeRunSummary } from '@/test/fixtures';

import { viewOfWatchedRun } from './runView';

/**
 * F12 — `viewOfWatchedRun`'s own tests, pure (no rendering, no query client, no router). Relocated
 * from `TailorPanel.test.tsx` (deleted at F11, `433dfc1`), which exercised these same derivations
 * only indirectly, through a full render of a component that has since been deleted. The cases
 * carried forward are the ones that are genuinely about *this function*'s decisions — not about
 * whether `useTailoringRun`'s poller actually stops issuing requests (that mechanism is
 * `RunPage.test.tsx`'s and `WorkspacePage.test.tsx`'s to cover) and not about `runReadErrorCopy`'s
 * own copy-per-code table (`apiErrorCopy.test.ts` already owns that) — only that `viewOfWatchedRun`
 * wires the result into the `unreadable` variant untouched.
 */

const RUN_ID = '0192f0a1-cccc-7000-8000-00000000cccc';

describe('viewOfWatchedRun', () => {
  it('AC-30: a queued or running run is "working"', () => {
    const view = viewOfWatchedRun(RUN_ID, makeRun({ id: RUN_ID, status: 'running' }), null, null);

    expect(view).toEqual({ kind: 'working', runId: RUN_ID, status: 'running' });
  });

  it('AC-30: a run gone from running to succeeded is "succeeded", carrying both documents', () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'final cv',
      cover_letter: 'final letter',
      tailored_cv_character_count: 8,
      cover_letter_character_count: 12,
    });

    const view = viewOfWatchedRun(RUN_ID, run, null, null);

    expect(view).toEqual({
      kind: 'succeeded',
      tailoredCv: { text: 'final cv', characterCount: 8 },
      coverLetter: { text: 'final letter', characterCount: 12 },
    });
  });

  it('AC-30: a run gone from running to failed is "failed", carrying its reason and retryable flag', () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'failed',
      failure_reason: 'llm_timed_out',
      retryable: true,
    });

    const view = viewOfWatchedRun(RUN_ID, run, null, null);

    expect(view).toEqual({ kind: 'failed', reason: 'llm_timed_out', retryable: true });
  });

  it('AC-28 (pure half): the working state carries no failure reason at all', () => {
    const view = viewOfWatchedRun(RUN_ID, makeRun({ id: RUN_ID, status: 'queued' }), null, null);

    expect(view.kind).toBe('working');
    expect(view).not.toHaveProperty('reason');
    expect(view).not.toHaveProperty('retryable');
  });

  it('the detail outranks the summary: a stale "succeeded" summary does not override a running detail', () => {
    const detail = makeRun({ id: RUN_ID, status: 'running' });
    const staleSummary = makeRunSummary({ id: RUN_ID, status: 'succeeded' });

    const view = viewOfWatchedRun(RUN_ID, detail, null, staleSummary);

    expect(view).toEqual({ kind: 'working', runId: RUN_ID, status: 'running' });
  });

  it('a summary is used only while it describes this run', () => {
    const summaryForAnotherRun = makeRunSummary({ id: 'a-different-run', status: 'succeeded' });

    const view = viewOfWatchedRun(RUN_ID, undefined, null, summaryForAnotherRun);

    expect(view).toEqual({ kind: 'loading' });
  });

  it('a summary for this run is read only until the detail answers', () => {
    const summary = makeRunSummary({ id: RUN_ID, status: 'running' });

    const view = viewOfWatchedRun(RUN_ID, undefined, null, summary);

    expect(view).toEqual({ kind: 'working', runId: RUN_ID, status: 'running' });
  });

  it('an error outranks data: a 404 wins even while the last "running" response is still cached', () => {
    const staleData = makeRun({ id: RUN_ID, status: 'running' });
    const error = new ApiError(404, 'irrelevant server prose', 'tailoring_run_not_found');

    const view = viewOfWatchedRun(RUN_ID, staleData, error, null);

    expect(view).toEqual({
      kind: 'unreadable',
      message: "We couldn't find that run.",
      canCheckAgain: false,
    });
  });

  it('decision (b): a run 404ing mid-poll (purged) is unreadable, with no way to check again', () => {
    const error = new ApiError(404, 'irrelevant server prose', 'tailoring_run_not_found');

    const view = viewOfWatchedRun(RUN_ID, undefined, error, null);

    expect(view).toEqual({
      kind: 'unreadable',
      message: "We couldn't find that run.",
      canCheckAgain: false,
    });
  });

  it('decision (b): a run 401ing mid-poll (session expired) is unreadable, with no way to check again', () => {
    const error = new ApiError(401, 'irrelevant server prose', 'guest_session_expired');

    const view = viewOfWatchedRun(RUN_ID, undefined, error, null);

    expect(view).toEqual({
      kind: 'unreadable',
      message: 'Your session has expired. Upload your CV again.',
      canCheckAgain: false,
    });
  });

  it('gap (b): exhausted transient retries are unreadable, but honestly offer to check again', () => {
    const error = new TypeError('Failed to fetch');

    const view = viewOfWatchedRun(RUN_ID, undefined, error, null);

    expect(view).toEqual({
      kind: 'unreadable',
      message: 'We lost contact with your tailoring run. It may still be working.',
      canCheckAgain: true,
    });
  });
});
