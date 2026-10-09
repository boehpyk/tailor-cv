import { useEffect, useReducer, useState } from 'react';
import { holdDeadline } from './hold';

/**
 * The hooks over `hold.ts` (plan §0.3): a hold is component state **derived** from one response,
 * never a store, a cache entry or browser storage. The only effect is the 1 s tick and the
 * exact-deadline timeout — synchronizing with time, which is outside React.
 */

export interface Hold {
  readonly held: boolean;
  /** Whole seconds left, derived from `Date.now()` on every render; 0 when not held. */
  readonly remainingSeconds: number;
}

export interface ErrorHold extends Hold {
  /** The deadline `holdDeadline` gave the error at receipt, for the copy; `null` when none. */
  readonly deadlineMs: number | null;
}

const TICK_MS = 1000;

/**
 * AC-7: held while `Date.now() < deadlineMs`; `null` (or a deadline already past) holds nothing and
 * arms no timer.
 *
 * `held` and `remainingSeconds` are read from the clock on **every render**, not kept in state: the
 * timers only ask React to render again. So a background tab whose timers were throttled is right on
 * its first render back, and nothing can drift from the deadline.
 */
export function useHold(deadlineMs: number | null): Hold {
  const [, tick] = useReducer((n: number) => n + 1, 0);

  useEffect(() => {
    if (deadlineMs === null) {
      return;
    }
    const remainingMs = deadlineMs - Date.now();
    if (remainingMs <= 0) {
      return;
    }
    // The interval drives the per-second count; the timeout lands the release on the exact
    // instant, rather than up to 999 ms after it, and stops the interval with it.
    const interval = setInterval(tick, TICK_MS);
    const release = setTimeout(() => {
      clearInterval(interval);
      tick();
    }, remainingMs);
    return () => {
      clearInterval(interval);
      clearTimeout(release);
    };
  }, [deadlineMs]);

  const remainingMs = deadlineMs === null ? 0 : deadlineMs - Date.now();
  // `<`, not `<=`: at exactly the deadline the control is back (the test's mutation note).
  return remainingMs > 0
    ? { held: true, remainingSeconds: Math.ceil(remainingMs / TICK_MS) }
    : { held: false, remainingSeconds: 0 };
}

interface Seen {
  readonly error: unknown;
  readonly atMs: number;
}

/**
 * `useHold` over the deadline of `error`, stamped when that error object first arrived.
 *
 * The arrival time is state adjusted **during render** when the error changes — React's documented
 * "adjusting some state when a prop changes" pattern — never copied in by an effect, which would
 * render once with the stale hold first. A second 429 is a new object and restarts the hold;
 * `mutation.reset()` makes it `null` and releases it.
 */
export function useErrorHold(error: unknown): ErrorHold {
  const [seen, setSeen] = useState<Seen>(() => ({ error, atMs: Date.now() }));
  let current = seen;
  if (error !== seen.error) {
    current = { error, atMs: Date.now() };
    setSeen(current);
  }
  const deadlineMs = holdDeadline(current.error, current.atMs);
  return { ...useHold(deadlineMs), deadlineMs };
}
