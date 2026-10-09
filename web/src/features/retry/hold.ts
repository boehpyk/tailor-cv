import type { FetchStatus, QueryStatus } from '@tanstack/react-query';

/**
 * The pure half of slice 3.3 (ADR-0031): when a refused control may be used again, how to say so,
 * and what a poller's own retry state means for the line under it. No React, no clock of its own —
 * every "now" is a parameter, so each function is a truth table.
 *
 * T7 SKELETON: every body returns a wrong-but-typed value so the REDs fail on their assertions.
 */

/**
 * AC-4: the instant a 429 stops holding its control, or `null` for anything that is not a 429.
 * `Retry-After` absent or unparseable → 60 s; clamped to [1, 3600] s.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function holdDeadline(_error: unknown, _receivedAtMs: number): number | null {
  return null;
}

/** The two fields of a run `runHoldDeadline` reads — the full shape, the summary and history all carry them. */
export interface RunCooldownFields {
  readonly retry_not_before: string | null;
  readonly completed_at: string | null;
}

/**
 * The provider-busy cooldown after an `llm_rate_limited` run (AC-10), against the browser clock:
 * `min(retry_not_before, nowMs + (retry_not_before − completed_at))` — F-18's clamp, so a skewed
 * clock can never stretch the wait past what the server granted. Reads the API's instant; never
 * decides the 60 s.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function runHoldDeadline(_run: RunCooldownFields, _nowMs: number): number | null {
  return null;
}

export interface RetryPhraseOptions {
  readonly locale?: string;
  readonly timeZone?: string;
}

/**
 * AC-5: *"in N seconds"* when ≤ 90 s remain, *"at HH:MM"* (rounded up to the minute) beyond that,
 * *"now"* at or past the deadline. Locale and zone default to the browser's.
 */
/* eslint-disable @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN */
export function retryPhrase(
  _deadlineMs: number,
  _nowMs: number,
  _opts?: RetryPhraseOptions,
): string {
  return '';
}
/* eslint-enable @typescript-eslint/no-unused-vars */

/** What a poller is doing besides its normal tick — the input to `ConnectionNote`. */
export type Connection = 'ok' | 'retrying' | 'paused';

/** The three fields of a `useQuery` result `connectionOf` reads. */
export interface QueryConnectionFields {
  readonly status: QueryStatus;
  readonly fetchStatus: FetchStatus;
  readonly failureCount: number;
}

/**
 * AC-6: `'paused'` (offline) when TanStack paused the fetch; `'retrying'` while a transient failure
 * is being retried and the query has not given up; `'ok'` otherwise. `status === 'error'` is 1.3's
 * *lost contact*, not this function's.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function connectionOf(_q: QueryConnectionFields): Connection {
  return 'ok';
}
