/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the props are the signature qa's T27 tests compile against; T28 uses them and deletes this line. */
export interface TrackButtonProps {
  /** The signed-in user — roots the board query and the track mutation's key. */
  readonly userId: string;
  /** The **succeeded** run this button puts on the board. */
  readonly runId: string;
}

/**
 * **Add to board** / *"On your board · {stage}"* (AC-38) — a **container** over
 * `useBoardEntryForRun` and `useTrackApplication`, for a succeeded history row and a succeeded
 * account run page. The **caller** decides whether to render it (succeeded, account scope only); it
 * never asks.
 *
 * - On the board: *"On your board · {stage}"*, a link to `/board`.
 * - Else: **Add to board**; *"Adding…"* while pending (a same-tick double click posts once); 201 or
 *   409 `application_already_tracked` → *"On your board · To apply"*; other refusals → their copy,
 *   `role="alert"`.
 *
 * SKELETON (T26): renders nothing; T28 builds it.
 */
export function TrackButton(_props: TrackButtonProps): React.JSX.Element | null {
  return null;
}
