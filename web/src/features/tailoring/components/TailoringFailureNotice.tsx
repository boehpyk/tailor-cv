import { useId } from 'react';

import { HoldNote } from '@/features/retry/components/HoldNote';
import { useHold } from '@/features/retry/useHold';

import { failureCopyFor } from '../failureCopy';

import type { TailoringFailureReason } from '../types';

export interface TailoringFailureNoticeProps {
  readonly reason: TailoringFailureReason | null;
  /** The run's `retryable`, **exactly as the API sent it** (AC-13). Never derived here. */
  readonly retryable: boolean;
  /** Both inputs are on screen and usable, so a new run could actually be requested. */
  readonly canStart: boolean;
  readonly isStarting: boolean;
  /**
   * Until when *Try again* would be refused (slice 3.3, AC-10): the provider cooldown after an
   * `llm_rate_limited` run, or a 429 on the retry itself. `null` (the default) holds nothing.
   */
  readonly holdUntil?: number | null;
  readonly onRetry: () => void;
}

/**
 * **Error C — the run itself failed** — presentational.
 *
 * Keyed off `failure_reason` for the words and off `retryable` for the button, and those are two
 * different authorities on purpose. The words are UI copy (`failureCopy.ts`). Whether a failure is
 * worth paying for again is a business rule, so the API decides it and this component only reads the
 * answer: **Try again** exists when `retryable` is true, and for `llm_refused` or
 * `inputs_too_large` — where the same inputs will fail the same way — it does not.
 *
 * **Visibly and textually distinct from error B.** B (red) is "the API would not start a run"; C
 * (amber) is "a run was accepted and did not finish". The opening line says that in words, because
 * colour alone is not a distinction a screen reader or a colour-blind user can see. Amber matches
 * slice 1.1's error C for the same reason: the request worked, the outcome did not.
 */
export function TailoringFailureNotice({
  reason,
  retryable,
  canStart,
  isStarting,
  holdUntil = null,
  onRetry,
}: TailoringFailureNoticeProps): React.JSX.Element {
  const copy = failureCopyFor(reason);
  const hintId = useId();
  const hold = useHold(holdUntil);
  // While held, the hint is the hold's fixed sentence; once released, `HoldNote` says so instead,
  // and the generic hint ("Give it a minute") would contradict it.
  const hint = hold.held
    ? `You can try again ${hold.phrase ?? 'soon'}.`
    : hold.released
      ? null
      : copy.hint;

  return (
    <div
      role="alert"
      className="space-y-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2"
    >
      <p className="text-xs font-medium tracking-wide text-amber-700 uppercase">
        This tailoring run did not finish
      </p>
      <p className="text-sm font-medium text-amber-900">{copy.headline}</p>
      {hint !== null && (
        <p id={hintId} className="text-sm text-amber-800">
          {hint}
        </p>
      )}
      <HoldNote
        held={hold.held}
        remainingSeconds={hold.remainingSeconds}
        released={hold.released}
      />
      {retryable && (
        <button
          type="button"
          onClick={onRetry}
          disabled={!canStart || isStarting || hold.held}
          aria-describedby={hold.held ? hintId : undefined}
          className="rounded-md bg-amber-700 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-60"
        >
          {isStarting ? 'Starting…' : 'Try again'}
        </button>
      )}
    </div>
  );
}
