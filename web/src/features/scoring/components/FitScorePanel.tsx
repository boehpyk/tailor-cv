import { useId } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { HoldNote } from '@/features/retry/components/HoldNote';
import { useErrorHold } from '@/features/retry/useHold';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';

import {
  CHECK_BUTTON,
  FIT_SCORE_NOTICE,
  LOADING,
  LOAD_ERROR,
  NOT_READY,
  RETRY_LOAD,
  TRY_AGAIN,
  WORKING,
  holdSentence,
} from '../fitScoreCopy';
import { fitScoreView } from '../fitScoreView';
import { useFitScore } from '../hooks/useFitScore';
import { requestFitScoreMutationKey, useRequestFitScore } from '../hooks/useRequestFitScore';
import { FitScoreFailure } from './FitScoreFailure';
import { FitScoreResult } from './FitScoreResult';

import type { Hold } from '@/features/retry/useHold';
import type { FitScorePair } from '../types';

export interface FitScorePanelProps {
  /**
   * The pair on screen, from `launchInput(baseCv, jobPosting)`: the same two ids a tailoring run
   * would use, or `null` when either isn't ready.
   */
  readonly pair: FitScorePair | null;
}

/**
 * The fit-score section's body (container). The workspace owns the section and its `h2` (T40).
 *
 * Server state is the two hooks; the 429 hold is derived from the mutation's error during render
 * (ADR-0031 §5). No `useState`, no effect, no timer of its own. **Rendering issues no `POST`**: the
 * only call to `mutate` is the click handler, guarded by `isMutating` so a same-tick double click
 * sends one request (AC-48).
 *
 * `not_ready`, `empty`, `working`, `held` and `failed` all render one `CheckControl`, so its status
 * region and `HoldNote` stay mounted across them: *Checking…* fills a region that was already there,
 * and so does *You can try again now.* when a hold ends.
 */
export function FitScorePanel({ pair }: FitScorePanelProps): React.JSX.Element {
  const map = useScopeMap();
  const queryClient = useQueryClient();
  const read = useFitScore(pair);
  const request = useRequestFitScore();
  const hold = useErrorHold(request.error);

  const view = fitScoreView({
    hasPair: pair !== null,
    read: { status: read.status, data: read.data },
    request: { isPending: request.isPending, error: request.error },
    held: hold.held,
  });

  function check(): void {
    if (pair === null) {
      return;
    }
    if (queryClient.isMutating({ mutationKey: requestFitScoreMutationKey(map) }) > 0) {
      return;
    }
    request.mutate(pair);
  }

  // A guest whose session lapsed: tailoring's sentence, and no *Try again* that would fail the same way.
  const sessionEnded =
    request.error instanceof ApiError && request.error.code === 'guest_session_expired';

  switch (view.kind) {
    case 'succeeded':
      return <FitScoreResult score={view.score} scope={map.kind} />;
    case 'loading':
      return (
        <p role="status" className="text-sm text-slate-500">
          {LOADING}
        </p>
      );
    case 'load_error':
      return (
        <div
          role="alert"
          className="space-y-2 rounded-lg border border-danger-200 bg-danger-50 px-4 py-3"
        >
          <p className="text-sm font-medium text-danger-600">{LOAD_ERROR}</p>
          <button
            type="button"
            onClick={() => void read.refetch()}
            className="inline-flex min-h-11 items-center rounded-lg border border-danger-200 bg-white px-3 text-sm font-semibold text-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9"
          >
            {RETRY_LOAD}
          </button>
        </div>
      );
    case 'refused':
    case 'limit':
    case 'busy':
      return (
        <FitScoreFailure
          view={view}
          scope={map.kind}
          receivedAtMs={hold.receivedAtMs}
          sessionEnded={false}
        />
      );
    case 'failed':
      if (sessionEnded) {
        return (
          <FitScoreFailure
            view={view}
            scope={map.kind}
            receivedAtMs={hold.receivedAtMs}
            sessionEnded
          />
        );
      }
      return (
        <CheckControl
          label={TRY_AGAIN}
          reason={null}
          working={false}
          hold={hold}
          onCheck={check}
          failure={
            <FitScoreFailure
              view={view}
              scope={map.kind}
              receivedAtMs={hold.receivedAtMs}
              sessionEnded={false}
            />
          }
        />
      );
    case 'not_ready':
    case 'empty':
    case 'working':
    case 'held':
      return (
        <CheckControl
          label={CHECK_BUTTON}
          reason={
            view.kind === 'not_ready'
              ? NOT_READY
              : view.kind === 'held'
                ? holdSentence(hold.phrase)
                : null
          }
          working={view.kind === 'working'}
          hold={hold}
          onCheck={check}
          failure={null}
        />
      );
  }
}

interface CheckControlProps {
  readonly label: string;
  /** Why the button is disabled, as its accessible description; `null` when it may be pressed. */
  readonly reason: string | null;
  readonly working: boolean;
  readonly hold: Hold;
  readonly onCheck: () => void;
  /** A finished request's failure, shown above the notice (`failed` only). */
  readonly failure: React.ReactNode;
}

/**
 * The notice, the button and its states. The notice is the button's description whenever it can be
 * pressed (AC-47); a disabled button is described by its reason instead. The status region mounts
 * empty and fills while the `POST` is in flight.
 */
function CheckControl({
  label,
  reason,
  working,
  hold,
  onCheck,
  failure,
}: CheckControlProps): React.JSX.Element {
  const noticeId = useId();
  const reasonId = useId();
  return (
    <div className="space-y-3">
      {failure}
      <p id={noticeId} className="text-sm text-slate-600">
        {FIT_SCORE_NOTICE}
      </p>
      <button
        type="button"
        onClick={onCheck}
        disabled={reason !== null || working}
        aria-describedby={reason !== null ? reasonId : noticeId}
        className="min-h-11 rounded-lg bg-brand-600 px-6 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 max-sm:w-full"
      >
        {label}
      </button>
      {reason !== null && (
        <p id={reasonId} className="text-sm text-slate-600">
          {reason}
        </p>
      )}
      <p role="status" className="text-sm text-slate-600">
        {working ? WORKING : null}
      </p>
      <HoldNote
        held={hold.held}
        remainingSeconds={hold.remainingSeconds}
        released={hold.released}
      />
    </div>
  );
}
