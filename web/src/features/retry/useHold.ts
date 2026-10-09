/**
 * The hooks over `hold.ts` (plan §0.3): a hold is component state **derived** from one response,
 * never a store, a cache entry or browser storage. The only effect (in GREEN) is the 1 s tick and
 * the exact-deadline timeout — synchronizing with time, which is outside React.
 *
 * T7 SKELETON: never holds.
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

/** AC-7: held while `Date.now() < deadlineMs`; `null` holds nothing and arms no timer. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function useHold(_deadlineMs: number | null): Hold {
  return { held: false, remainingSeconds: 0 };
}

/** `useHold` over the deadline of `error`, stamped when that error object first arrived. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function useErrorHold(_error: unknown): ErrorHold {
  return { held: false, remainingSeconds: 0, deadlineMs: null };
}
