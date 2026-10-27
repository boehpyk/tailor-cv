import { retryPhrase } from '@/features/retry/hold';

import { ConflictNotice } from './ConflictNotice';
import { documentProblemCopy, saveStateCopy } from '../saveState';

import type { SaveState } from '../saveState';

export interface SaveIndicatorProps {
  readonly state: SaveState;
}

/** The sentence for a state, with the 422 reason after the colon (AC-31: *Can't save: <reason>*). */
function sentenceFor(state: SaveState): string {
  switch (state.kind) {
    case 'invalid':
      return `${saveStateCopy.invalid}: ${documentProblemCopy[state.problem]}`;
    case 'paused':
      // AC-16: the instant the hook stamped at receipt, through `retryPhrase` — never the raw
      // header. Worded as of the receipt (`untilMs − retryAfterSeconds`), never `Date.now()`: this
      // is a live region, and a re-render must not reword it (AC-23).
      return `${saveStateCopy.paused} — we'll save again automatically ${retryPhrase(
        state.untilMs,
        state.untilMs - state.retryAfterSeconds * 1000,
      )}.`;
    case 'saved':
    case 'dirty':
    case 'saving':
    case 'failed':
    case 'conflict':
    case 'expired':
      return saveStateCopy[state.kind];
  }
}

/**
 * The save state, as a sentence — presentational. The sentence is `role="status"` so a screen
 * reader hears *Saving…* become *Saved* without the focus moving; the controls a state offers
 * (*Retry* on `failed`, the two choices on `conflict`) sit beside it, outside the live region, so
 * they are not re-announced with every change.
 *
 * The discriminated union is what makes this honest: `retry` exists only on `failed`, so a
 * *Retry* button cannot be rendered next to *Saving…* — the working state offers no control, as
 * AC-31's "still working vs failed" rule requires of writes.
 */
export function SaveIndicator({ state }: SaveIndicatorProps): React.JSX.Element {
  const tone =
    state.kind === 'failed' || state.kind === 'conflict' || state.kind === 'expired'
      ? 'text-danger-600'
      : state.kind === 'invalid' || state.kind === 'paused'
        ? 'text-amber-700'
        : 'text-slate-500';

  return (
    <div className="flex flex-wrap items-center gap-3">
      <p role="status" className={`text-sm ${tone}`}>
        {sentenceFor(state)}
      </p>
      {state.kind === 'failed' && (
        <button
          type="button"
          onClick={state.retry}
          className="min-h-11 rounded-full border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-900 hover:bg-surface-sunk sm:min-h-8 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
        >
          Retry
        </button>
      )}
      {state.kind === 'conflict' && (
        <ConflictNotice onLoadLatest={state.loadLatest} onKeepMine={state.keepMine} />
      )}
    </div>
  );
}
