/**
 * A held control's note (plan §5, AC-23): the fixed hold sentence, an `aria-hidden` per-second
 * count when ≤ 90 s remain, and a `role="status"` *"You can try again now."* once the hold ends.
 * Presentational: the hold comes from `useHold` / `useErrorHold` in the owner.
 *
 * T7 SKELETON: renders nothing.
 */
export interface HoldNoteProps {
  /** The fixed sentence naming the wait (from `retryPhrase`), shown while held. */
  readonly sentence: string;
  /** The hold's deadline; `null` means there was never a hold, so nothing (not even the end) shows. */
  readonly deadlineMs: number | null;
  readonly held: boolean;
  readonly remainingSeconds: number;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function HoldNote(_props: HoldNoteProps): React.JSX.Element | null {
  return null;
}
