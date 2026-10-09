import { useEffect, useReducer, useState } from 'react';
import { holdDeadline, retryPhrase, runHoldDeadline } from './hold';

import type { RunCooldownFields } from './hold';

/**
 * The hooks over `hold.ts` (plan §0.3): a hold is component state **derived** from one response,
 * never a store, a cache entry or browser storage. The only effect is the 1 s tick and the
 * exact-deadline timeout — synchronizing with time, which is outside React.
 */

export interface Hold {
  readonly held: boolean;
  /** Whole seconds left, derived from `Date.now()` on every render; 0 when not held. */
  readonly remainingSeconds: number;
  /**
   * The wait in words (*"in 45 seconds"*, *"at 14:03"*), **worded once, when the deadline first
   * appeared**, so a sentence built from it is fixed text: it never ticks inside a live region
   * (AC-23). `null` whenever nothing is held — no deadline, or one that has passed — so a sentence
   * built from it names no wait beside `HoldNote`'s *"You can try again now."*.
   */
  readonly phrase: string | null;
  /**
   * This hook saw the deadline hold and it has since passed — when *"You can try again now."*
   * belongs on screen. A deadline already past when it first appeared (a run failed hours ago) was
   * never a hold the user saw, so it releases nothing.
   */
  readonly released: boolean;
}

export interface ErrorHold extends Hold {
  /** The deadline `holdDeadline` gave the error at receipt, for the copy; `null` when none. */
  readonly deadlineMs: number | null;
  /**
   * When the error arrived. A copy-only surface words the wait with `retryPhrase(deadlineMs,
   * receivedAtMs)` so its sentence is fixed text, never `Date.now()` at render (AC-16, AC-23).
   */
  readonly receivedAtMs: number;
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
  // When this deadline first appeared, and whether it was ever seen holding. Adjusted during render
  // when the deadline changes (React's "adjusting state when a prop changes"), never by an effect.
  const [seen, setSeen] = useState<SeenDeadline>(() => firstSight(deadlineMs));
  let current = seen;
  if (seen.deadlineMs !== deadlineMs) {
    current = firstSight(deadlineMs);
    setSeen(current);
  }

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
  const held = remainingMs > 0;
  if (held && !current.held) {
    current = { ...current, held: true };
    setSeen(current);
  }
  const phrase = held && deadlineMs !== null ? retryPhrase(deadlineMs, current.atMs) : null;
  return held
    ? { held, remainingSeconds: Math.ceil(remainingMs / TICK_MS), phrase, released: false }
    : { held, remainingSeconds: 0, phrase, released: current.held };
}

interface SeenDeadline {
  readonly deadlineMs: number | null;
  readonly atMs: number;
  readonly held: boolean;
}

function firstSight(deadlineMs: number | null): SeenDeadline {
  const atMs = Date.now();
  return { deadlineMs, atMs, held: deadlineMs !== null && atMs < deadlineMs };
}

/**
 * `runHoldDeadline` for the run on screen, **computed once per cooldown** rather than per render:
 * F-18's clamp reads the browser clock, so recomputing it on every tick would move a skewed
 * deadline forward for ever. Keyed on the two instants the API sent.
 */
export function useRunHoldDeadline(run: RunCooldownFields | null): number | null {
  const key =
    run === null || run.retry_not_before === null
      ? null
      : `${run.retry_not_before}|${run.completed_at ?? ''}`;
  const [seen, setSeen] = useState<{ key: string | null; deadlineMs: number | null }>(() => ({
    key,
    deadlineMs: run === null ? null : runHoldDeadline(run, Date.now()),
  }));
  if (seen.key === key) {
    return seen.deadlineMs;
  }
  const deadlineMs = run === null ? null : runHoldDeadline(run, Date.now());
  setSeen({ key, deadlineMs });
  return deadlineMs;
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
  return { ...useHold(deadlineMs), deadlineMs, receivedAtMs: current.atMs };
}
