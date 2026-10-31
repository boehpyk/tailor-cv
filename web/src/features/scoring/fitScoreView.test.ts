import { describe, expect, it } from 'vitest';
import { ApiError } from '../../api/client';
import { fitScoreView, type FitScoreViewInput } from './fitScoreView';
import type { FitScore, FitScoreForPair } from './types';

// Slice 5.1, AC-45 / technical plan §6 and its T33-T35 precedence. Pure: inputs in, one view out.

const SCORE: FitScore = {
  id: '0190a000-0000-7000-8000-000000000001',
  score_percent: 62,
  gaps: ['Kubernetes'],
  scored_at: '2026-10-31T10:00:00Z',
  expires_at: null,
};
const STORED: FitScoreForPair = { fit_score: SCORE };
const NONE: FitScoreForPair = { fit_score: null };

/** A ready pair, an answered empty read, no request: the `empty` view. Each case overrides. */
function input(over: {
  hasPair?: boolean;
  read?: FitScoreViewInput['read'];
  request?: Partial<FitScoreViewInput['request']>;
  held?: boolean;
}): FitScoreViewInput {
  return {
    hasPair: over.hasPair ?? true,
    read: over.read ?? { status: 'success', data: NONE },
    request: { isPending: false, error: null, ...over.request },
    held: over.held ?? false,
  };
}

const api = (status: number, code: string | null, details: Record<string, unknown> = {}) =>
  new ApiError(status, 'x', code, details);

describe('fitScoreView: one kind per state (AC-45)', () => {
  it('no pair is not_ready', () => {
    expect(fitScoreView(input({ hasPair: false }))).toEqual({ kind: 'not_ready' });
  });

  it('a pending read is loading', () => {
    expect(fitScoreView(input({ read: { status: 'pending', data: undefined } }))).toEqual({
      kind: 'loading',
    });
  });

  it('a failed read with no data is load_error', () => {
    expect(fitScoreView(input({ read: { status: 'error', data: undefined } }))).toEqual({
      kind: 'load_error',
    });
  });

  it('an answered read with fit_score null is empty', () => {
    expect(fitScoreView(input({}))).toEqual({ kind: 'empty' });
  });

  it('a request in flight is working', () => {
    expect(fitScoreView(input({ request: { isPending: true } }))).toEqual({ kind: 'working' });
  });

  it('a stored score is succeeded and carries the score', () => {
    expect(fitScoreView(input({ read: { status: 'success', data: STORED } }))).toEqual({
      kind: 'succeeded',
      score: SCORE,
    });
  });

  it('a 429 still holding is held', () => {
    expect(
      fitScoreView(input({ request: { error: api(429, 'rate_limited') }, held: true })),
    ).toEqual({ kind: 'held' });
  });
});

describe('fitScoreView: request errors', () => {
  it.each([
    ['503 scoring_failed', api(503, 'scoring_failed')],
    ['503 service_unavailable', api(503, 'service_unavailable')],
    ['503 rate_limit_unavailable', api(503, 'rate_limit_unavailable')],
    ['a network failure (not an ApiError)', new TypeError('Failed to fetch')],
    ['413', api(413, 'payload_too_large')],
    ['422 validation_error', api(422, 'validation_error')],
    ['an unknown code', api(500, 'something_new')],
    ['an envelope-less error', api(502, null)],
  ])('%s is failed', (_name, error) => {
    expect(fitScoreView(input({ request: { error } }))).toEqual({ kind: 'failed' });
  });

  it.each(['scoring_refused', 'inputs_too_large'] as const)(
    '422 %s is refused with its code',
    (code) => {
      expect(fitScoreView(input({ request: { error: api(422, code) } }))).toEqual({
        kind: 'refused',
        code,
      });
    },
  );

  it.each([
    ['base_cv_not_found', 404],
    ['job_posting_not_found', 404],
    ['base_cv_not_extracted', 409],
  ])('%s (%i) is not_ready', (code, status) => {
    expect(fitScoreView(input({ request: { error: api(status, code) } }))).toEqual({
      kind: 'not_ready',
    });
  });

  it('409 too_many_fit_scores carries retry_not_before', () => {
    const at = '2026-10-31T14:00:00+00:00';
    const error = api(409, 'too_many_fit_scores', { limit: 10, retry_not_before: at });
    expect(fitScoreView(input({ request: { error } }))).toEqual({
      kind: 'limit',
      retryNotBefore: at,
    });
  });

  it('409 too_many_fit_scores for a guest carries retryNotBefore null', () => {
    const error = api(409, 'too_many_fit_scores', { limit: 3, retry_not_before: null });
    expect(fitScoreView(input({ request: { error } }))).toEqual({
      kind: 'limit',
      retryNotBefore: null,
    });
  });

  it('409 too_many_fit_scores without retry_not_before carries retryNotBefore null', () => {
    const error = api(409, 'too_many_fit_scores', { limit: 3 });
    expect(fitScoreView(input({ request: { error } }))).toEqual({
      kind: 'limit',
      retryNotBefore: null,
    });
  });

  it('503 scoring_busy is busy', () => {
    expect(fitScoreView(input({ request: { error: api(503, 'scoring_busy') } }))).toEqual({
      kind: 'busy',
    });
  });

  it('an expired 429 hold falls through to empty', () => {
    expect(
      fitScoreView(input({ request: { error: api(429, 'rate_limited') }, held: false })),
    ).toEqual({ kind: 'empty' });
  });

  it('an expired 429 hold over a stored score is succeeded', () => {
    const view = fitScoreView(
      input({
        read: { status: 'success', data: STORED },
        request: { error: api(429, 'rate_limited') },
      }),
    );
    expect(view).toEqual({ kind: 'succeeded', score: SCORE });
  });
});

describe('fitScoreView: precedence, highest first', () => {
  const failed = api(503, 'scoring_failed');
  const held429 = api(429, 'rate_limited');

  it('not_ready beats working', () => {
    expect(fitScoreView(input({ hasPair: false, request: { isPending: true } }))).toEqual({
      kind: 'not_ready',
    });
  });

  it('not_ready beats a stored score', () => {
    expect(
      fitScoreView(input({ hasPair: false, read: { status: 'success', data: STORED } })),
    ).toEqual({ kind: 'not_ready' });
  });

  it('working beats held', () => {
    expect(
      fitScoreView(input({ request: { isPending: true, error: held429 }, held: true })),
    ).toEqual({ kind: 'working' });
  });

  it('working beats a previous failure (AC-46: only a finished request shows one)', () => {
    expect(fitScoreView(input({ request: { isPending: true, error: failed } }))).toEqual({
      kind: 'working',
    });
  });

  it('working beats a stored score', () => {
    expect(
      fitScoreView(
        input({ read: { status: 'success', data: STORED }, request: { isPending: true } }),
      ),
    ).toEqual({ kind: 'working' });
  });

  it('a stored score beats an active hold (AC-46: the score already exists)', () => {
    expect(
      fitScoreView(
        input({
          read: { status: 'success', data: STORED },
          request: { error: held429 },
          held: true,
        }),
      ),
    ).toEqual({ kind: 'succeeded', score: SCORE });
  });

  it('held beats a pending read', () => {
    expect(
      fitScoreView(
        input({
          read: { status: 'pending', data: undefined },
          request: { error: held429 },
          held: true,
        }),
      ),
    ).toEqual({ kind: 'held' });
  });

  it.each([
    ['failed', failed],
    ['refused', api(422, 'scoring_refused')],
    ['limit', api(409, 'too_many_fit_scores', { retry_not_before: null })],
    ['busy', api(503, 'scoring_busy')],
  ])('a stored score beats a %s request outcome', (_name, error) => {
    expect(
      fitScoreView(input({ read: { status: 'success', data: STORED }, request: { error } })),
    ).toEqual({ kind: 'succeeded', score: SCORE });
  });

  it("the request's own outcome beats a pending read", () => {
    expect(
      fitScoreView(
        input({ read: { status: 'pending', data: undefined }, request: { error: failed } }),
      ),
    ).toEqual({ kind: 'failed' });
  });

  it("the request's own outcome beats load_error", () => {
    expect(
      fitScoreView(
        input({ read: { status: 'error', data: undefined }, request: { error: failed } }),
      ),
    ).toEqual({ kind: 'failed' });
  });

  it('a failed refetch with stale data is succeeded (stale data wins; reads retry)', () => {
    expect(fitScoreView(input({ read: { status: 'error', data: STORED } }))).toEqual({
      kind: 'succeeded',
      score: SCORE,
    });
  });

  it('a failed refetch with stale empty data is empty, not load_error', () => {
    expect(fitScoreView(input({ read: { status: 'error', data: NONE } }))).toEqual({
      kind: 'empty',
    });
  });

  it('a stored score beats loading (a background refetch)', () => {
    expect(fitScoreView(input({ read: { status: 'pending', data: STORED } }))).toEqual({
      kind: 'succeeded',
      score: SCORE,
    });
  });
});
