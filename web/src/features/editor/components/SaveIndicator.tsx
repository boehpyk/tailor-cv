import type { SaveState } from '../saveState';

export interface SaveIndicatorProps {
  readonly state: SaveState;
}

/**
 * The save state, as a sentence — presentational, `role="status"` so a screen reader hears
 * *Saving…* become *Saved* without the focus moving.
 *
 * Skeleton (F8): an empty status region. F10c renders `saveStateCopy[state.kind]`, the Retry
 * button on `failed` and the `ConflictNotice` on `conflict`.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- skeleton: read in F10c
export function SaveIndicator(_props: SaveIndicatorProps): React.JSX.Element {
  return <p role="status" className="text-sm text-slate-500" />;
}
