import type { FetchStatus, QueryStatus } from '@tanstack/react-query';
import { ApiError } from '@/api/client';

/** F-10: a 429 that named no usable window holds for a minute. */
const DEFAULT_HOLD_SECONDS = 60;
/** F-11: never longer than the API's longest window (an hour), never shorter than a second. */
const MIN_HOLD_SECONDS = 1;
const MAX_HOLD_SECONDS = 3600;
/** At or under this, the wait is a seconds count; above it, a clock time (§0.5, OQ-4). */
const SECONDS_PHRASE_LIMIT_MS = 90_000;
const MINUTE_MS = 60_000;

/**
 * The pure half of slice 3.3 (ADR-0031): when a refused control may be used again, how to say so,
 * and what a poller's own retry state means for the line under it. No React, no clock of its own —
 * every "now" is a parameter, so each function is a truth table.
 */

/**
 * AC-4: the instant a 429 stops holding its control, or `null` for anything that is not a 429.
 * `Retry-After` absent or unparseable → 60 s; clamped to [1, 3600] s.
 */
export function holdDeadline(error: unknown, receivedAtMs: number): number | null {
  if (!(error instanceof ApiError) || error.status !== 429) {
    return null;
  }
  const seconds = Math.min(
    MAX_HOLD_SECONDS,
    Math.max(MIN_HOLD_SECONDS, error.retryAfterSeconds ?? DEFAULT_HOLD_SECONDS),
  );
  return receivedAtMs + seconds * 1000;
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
export function runHoldDeadline(run: RunCooldownFields, nowMs: number): number | null {
  if (run.retry_not_before === null) {
    return null;
  }
  const notBefore = Date.parse(run.retry_not_before);
  if (Number.isNaN(notBefore)) {
    return null;
  }
  const completed = run.completed_at === null ? Number.NaN : Date.parse(run.completed_at);
  if (Number.isNaN(completed)) {
    return notBefore;
  }
  // The server's instant, but never further from the browser's "now" than the server granted.
  return Math.min(notBefore, nowMs + (notBefore - completed));
}

export interface RetryPhraseOptions {
  readonly locale?: string;
  readonly timeZone?: string;
}

/**
 * AC-5: *"in N seconds"* when ≤ 90 s remain, *"at HH:MM"* (rounded up to the minute) beyond that,
 * *"now"* at or past the deadline. Locale and zone default to the browser's.
 */
export function retryPhrase(deadlineMs: number, nowMs: number, opts?: RetryPhraseOptions): string {
  const remainingMs = deadlineMs - nowMs;
  if (remainingMs <= 0) {
    return 'now';
  }
  if (remainingMs <= SECONDS_PHRASE_LIMIT_MS) {
    const seconds = Math.ceil(remainingMs / 1000);
    return `in ${String(seconds)} ${seconds === 1 ? 'second' : 'seconds'}`;
  }
  // Rounded UP to the minute, so the time named is never before the deadline.
  const at = Math.ceil(deadlineMs / MINUTE_MS) * MINUTE_MS;
  // `hour: '2-digit'`, not the plan's `'numeric'`: AC-5 says HH:MM, and en-GB's `'numeric'` hour
  // drops the leading zero (7:24, not 07:24).
  const time = new Intl.DateTimeFormat(opts?.locale, {
    hour: '2-digit',
    minute: '2-digit',
    ...(opts?.timeZone === undefined ? {} : { timeZone: opts.timeZone }),
  }).format(at);
  return `at ${time}`;
}

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
export function connectionOf(q: QueryConnectionFields): Connection {
  if (q.fetchStatus === 'paused') {
    return 'paused';
  }
  return q.status !== 'error' && q.failureCount >= 1 ? 'retrying' : 'ok';
}
