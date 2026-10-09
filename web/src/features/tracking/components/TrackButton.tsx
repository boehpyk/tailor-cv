import { useQueryClient } from '@tanstack/react-query';
import { useId } from 'react';
import { Link } from 'react-router';

import { useErrorHold } from '@/features/retry/useHold';

import { variablesNameId } from '../hooks/boardCache';
import { trackApplicationMutationKey } from '../hooks/trackingKeys';
import { useBoardEntryForRun } from '../hooks/useBoardEntryForRun';
import { useTrackApplication } from '../hooks/useTrackApplication';
import { TRACK_LABEL, TRACK_PENDING_LABEL, onBoardLabel, trackFailureCopy } from '../trackingCopy';

import type { Stage } from '../types';

export interface TrackButtonProps {
  /** The signed-in user — roots the board query and the track mutation's key. */
  readonly userId: string;
  /** The **succeeded** run this button puts on the board. */
  readonly runId: string;
}

const LINK_CLASS = 'text-sm font-medium text-slate-900 underline underline-offset-2';

/**
 * **Add to board** / *"On your board · {stage}"* (AC-38) — a **container** over
 * `useBoardEntryForRun` and `useTrackApplication`, for a succeeded history row and a succeeded
 * account run page. The **caller** decides whether to render it (succeeded, account scope only); it
 * never asks.
 *
 * - On the board: *"On your board · {stage}"*, a link to `/board`. The stage is the board's, read
 *   through the shared board query, so a move on `/board` shows here on the next read.
 * - Else: **Add to board**; *"Adding…"* while pending (a same-tick double click posts once); 201 or
 *   409 `application_already_tracked` → the board is re-read, and the badge follows it; other
 *   refusals → their copy, `role="alert"`, and the button is back.
 *
 * A 201 also carries the new card's stage, so the badge shows even if that re-read fails — the
 * server said the card exists, which is a fact and not a guess.
 */
export function TrackButton({ userId, runId }: TrackButtonProps): React.JSX.Element {
  const entry = useBoardEntryForRun(userId, runId);
  const track = useTrackApplication(userId);
  const queryClient = useQueryClient();
  const errorId = useId();
  // Copy only (AC-16): a 429's wait, worded once from when it arrived. Nothing is held.
  const trackHold = useErrorHold(track.error);

  const trackedStage: Stage | null =
    entry?.stage ?? (track.data?.kind === 'tracked' ? track.data.application.stage : null);

  if (trackedStage !== null) {
    return (
      <Link to="/board" className={LINK_CLASS}>
        {onBoardLabel(trackedStage)}
      </Link>
    );
  }

  function add(): void {
    // `isPending` lags a same-tick double click; the mutation cache does not (T-29). Scoped to this
    // run, so adding one row does not refuse a click on another.
    const inFlight = queryClient.isMutating({
      mutationKey: trackApplicationMutationKey(userId),
      predicate: (mutation) => variablesNameId(mutation.state.variables, runId),
    });
    if (inFlight === 0) {
      track.mutate(runId);
    }
  }

  return (
    <span className="inline-flex flex-col gap-1">
      <button
        type="button"
        onClick={add}
        disabled={track.isPending}
        aria-describedby={track.isError ? errorId : undefined}
        className="self-start rounded-md border border-slate-300 bg-white px-3 py-1 text-sm font-medium text-slate-800 disabled:opacity-60"
      >
        {track.isPending ? TRACK_PENDING_LABEL : TRACK_LABEL}
      </button>
      {track.isError && (
        <span id={errorId} role="alert" className="text-sm text-red-700">
          {trackFailureCopy(track.error, trackHold.deadlineMs, trackHold.receivedAtMs)}
        </span>
      )}
    </span>
  );
}
