import { TRY_AGAIN_NOW } from '../retryCopy';

import type { Hold } from '../useHold';

/** Under this many seconds a visual count is shown; above it the sentence's clock time is enough. */
const COUNTDOWN_LIMIT_SECONDS = 90;

export type HoldNoteProps = Pick<Hold, 'held' | 'remainingSeconds' | 'released'> & {
  /** `false` when the note sits inside a status region already (`ExportControl`). */
  readonly announce?: boolean;
};

/**
 * The parts of a hold that are **not** its sentence (plan §5 a11y, AC-23). The sentence — *"You can
 * try again at 14:03."* — lives once in the owner's existing alert or disabled-reason text, as fixed
 * words. This adds:
 *
 * - while held and ≤ 90 s remain, a per-second count that is `aria-hidden`: a ticking number in a
 *   live region would be announced every second;
 * - once the hold ends, a `role="status"` *"You can try again now."* — polite, and **no focus
 *   move**: the control coming back is news, not an interruption. With `announce={false}` it
 *   joins the status region it sits in rather than nesting a second one.
 *
 * No animation, so nothing for `prefers-reduced-motion` to reduce.
 */
export function HoldNote({
  held,
  remainingSeconds,
  released,
  announce = true,
}: HoldNoteProps): React.JSX.Element | null {
  // A hold mounts its status region empty and fills it at the release: many screen readers announce
  // only a change inside a region that already existed, never one mounted with its text. Nothing
  // held, nothing released: no region at all, so an idle form carries no stray live region.
  if (!held && !released) {
    return null;
  }
  const releaseText = released ? TRY_AGAIN_NOW : null;
  return (
    <>
      {held && remainingSeconds <= COUNTDOWN_LIMIT_SECONDS && (
        <p aria-hidden="true" className="text-sm text-slate-500 tabular-nums">
          {String(remainingSeconds)} s
        </p>
      )}
      {announce ? (
        <p role="status" className="text-sm text-slate-700">
          {releaseText}
        </p>
      ) : (
        releaseText !== null && <span className="block text-slate-700">{releaseText}</span>
      )}
    </>
  );
}
