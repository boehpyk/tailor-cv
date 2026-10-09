import { describe, expect, it } from 'vitest';
import { ApiError } from '../../api/client';
import { connectionOf, holdDeadline, retryPhrase, runHoldDeadline } from './hold';

// 2023-11-14T22:13:20Z — a whole second, so every expected value below is plain arithmetic.
const NOW = 1_700_000_000_000;
const SECOND = 1000;

function tooMany(retryAfterSeconds: number | null): ApiError {
  return new ApiError(429, 'Too many.', 'rate_limited', {}, retryAfterSeconds);
}

describe('holdDeadline (AC-4)', () => {
  it.each([
    ['120 s', 120, NOW + 120 * SECOND],
    ['1 s', 1, NOW + SECOND],
    ['60 s', 60, NOW + 60 * SECOND],
    ['absent, HTTP-date or garbage (null) defaults to 60 s', null, NOW + 60 * SECOND],
    ['0 s is clamped up to 1 s', 0, NOW + SECOND],
    ['3600 s is the ceiling, kept', 3600, NOW + 3600 * SECOND],
    // Mutation note (3): removing the clamp makes this row red (F-11).
    ['999999 s is clamped down to 3600 s', 999_999, NOW + 3600 * SECOND],
  ])('a 429 with Retry-After %s', (_label, seconds, expected) => {
    expect(holdDeadline(tooMany(seconds), NOW)).toBe(expected);
  });

  it('reads the server-named window, not a constant', () => {
    // Mutation note (2): a holdDeadline that ignores `retryAfterSeconds` (always 60) keeps the
    // 60 s rows above green and goes red here: 120 s was asked for and 60 s was returned.
    expect(holdDeadline(tooMany(120), NOW)).not.toBe(holdDeadline(tooMany(60), NOW));
  });

  it.each([
    ['a 503', new ApiError(503, 'Down.', 'service_unavailable', {}, 30)],
    ['a 401', new ApiError(401, 'No.', 'invalid_access_token', {}, 30)],
    ['a 409', new ApiError(409, 'Clash.', 'tailoring_already_running')],
    ['a TypeError', new TypeError('Failed to fetch')],
    ['a string', 'rate_limited'],
    ['undefined', undefined],
    ['null', null],
  ])('%s never holds', (_label, error) => {
    expect(holdDeadline(error, NOW)).toBeNull();
  });
});

describe('runHoldDeadline (AC-10, F-18)', () => {
  const completed = NOW;
  const granted = completed + 60 * SECOND;
  const run = {
    retry_not_before: new Date(granted).toISOString(),
    completed_at: new Date(completed).toISOString(),
  };

  it('is the server instant when the browser clock agrees', () => {
    expect(runHoldDeadline(run, completed + 10 * SECOND)).toBe(granted);
  });

  it('a browser 10 minutes behind waits at most the 60 s granted, not 11 minutes', () => {
    const behind = completed - 600 * SECOND;
    expect(runHoldDeadline(run, behind)).toBe(behind + 60 * SECOND);
  });

  it('a browser 10 minutes ahead waits 0: the deadline is already past', () => {
    const ahead = completed + 600 * SECOND;
    const deadline = runHoldDeadline(run, ahead);
    expect(deadline).not.toBeNull();
    expect(deadline).toBeLessThanOrEqual(ahead);
  });

  it('is null for a run with no cooldown', () => {
    expect(
      runHoldDeadline({ retry_not_before: null, completed_at: run.completed_at }, NOW),
    ).toBeNull();
  });
});

describe('retryPhrase (AC-5)', () => {
  const opts = { locale: 'en-GB', timeZone: 'UTC' } as const;
  const phrase = (remainingMs: number) => retryPhrase(NOW + remainingMs, NOW, opts);

  it.each([
    [1, 'in 1 second'],
    [SECOND, 'in 1 second'],
    [1500, 'in 2 seconds'],
    [45 * SECOND, 'in 45 seconds'],
    [90 * SECOND, 'in 90 seconds'],
  ])('%i ms remaining reads "%s"', (remainingMs, expected) => {
    expect(phrase(remainingMs)).toBe(expected);
  });

  it.each([
    // NOW is 22:13:20 UTC. 91 s → 22:14:51, rounded UP to the minute: 22:15.
    [91 * SECOND, 'at 22:15'],
    // 100 s → 22:15:00 exactly: already a whole minute, never rounded to 22:16.
    [100 * SECOND, 'at 22:15'],
    [101 * SECOND, 'at 22:16'],
    [10 * 60 * SECOND, 'at 22:24'],
    [47 * 60 * SECOND + 40 * SECOND, 'at 23:01'], // crosses the hour
  ])('%i ms remaining reads "%s"', (remainingMs, expected) => {
    expect(phrase(remainingMs)).toBe(expected);
  });

  it('never names a time before the deadline', () => {
    // 22:14:51 is the deadline; "at 22:14" would be a lie that invites a refused retry.
    expect(phrase(91 * SECOND)).not.toBe('at 22:14');
  });

  it.each([0, -1, -3_600_000])('%i ms remaining (at or past the deadline) reads "now"', (ms) => {
    expect(phrase(ms)).toBe('now');
  });

  it('honours the given zone', () => {
    expect(
      retryPhrase(NOW + 10 * 60 * SECOND, NOW, { locale: 'en-GB', timeZone: 'Asia/Tokyo' }),
    ).toBe('at 07:24');
  });
});

describe('connectionOf (AC-6)', () => {
  it.each([
    ['paused', 'paused', 0, 'paused'],
    ['paused with failures still reads paused', 'paused', 3, 'paused'],
    ['retrying after one failed attempt', 'fetching', 1, 'retrying'],
    ['retrying on a pending query', 'fetching', 2, 'retrying'],
    ['a healthy fetch', 'fetching', 0, 'ok'],
    ['idle and quiet', 'idle', 0, 'ok'],
  ] as const)('%s', (_label, fetchStatus, failureCount, expected) => {
    const status = fetchStatus === 'idle' ? 'success' : 'pending';
    expect(connectionOf({ status, fetchStatus, failureCount })).toBe(expected);
  });

  it('a successful query that failed once and recovered is still retrying only while failureCount is set', () => {
    expect(connectionOf({ status: 'success', fetchStatus: 'fetching', failureCount: 1 })).toBe(
      'retrying',
    );
  });

  it('status "error" with failureCount 4 is ok: lost contact is 1.3\'s message, not this one', () => {
    // Mutation note (4): reading failureCount while status === 'error' shows two messages (AC-19).
    expect(connectionOf({ status: 'error', fetchStatus: 'idle', failureCount: 4 })).toBe('ok');
  });
});
