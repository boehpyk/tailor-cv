import { useId } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { useErrorHold } from '@/features/retry/useHold';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';

import { CHECK_BUTTON, FIT_SCORE_NOTICE } from '../fitScoreCopy';
import { fitScoreView } from '../fitScoreView';
import { useFitScore } from '../hooks/useFitScore';
import { requestFitScoreMutationKey, useRequestFitScore } from '../hooks/useRequestFitScore';
import { FitScoreFailure } from './FitScoreFailure';
import { FitScoreResult } from './FitScoreResult';

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
 * SKELETON (T35): each view renders a distinguishable stub. T39 builds the real markup against
 * T38's tests; `empty` already carries the notice beside the button, as D-5 requires.
 */
export function FitScorePanel({ pair }: FitScorePanelProps): React.JSX.Element {
  const map = useScopeMap();
  const queryClient = useQueryClient();
  const read = useFitScore(pair);
  const request = useRequestFitScore();
  const hold = useErrorHold(request.error);
  const noticeId = useId();

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

  switch (view.kind) {
    case 'succeeded':
      return <FitScoreResult score={view.score} scope={map.kind} />;
    case 'failed':
    case 'refused':
    case 'limit':
    case 'busy':
    case 'held':
      return <FitScoreFailure view={view} scope={map.kind} hold={hold} onRetry={check} />;
    case 'empty':
      return (
        <div data-fit-score-view={view.kind}>
          <p id={noticeId}>{FIT_SCORE_NOTICE}</p>
          <button type="button" onClick={check} aria-describedby={noticeId}>
            {CHECK_BUTTON}
          </button>
        </div>
      );
    case 'not_ready':
    case 'loading':
    case 'load_error':
    case 'working':
      return <div data-fit-score-view={view.kind} />;
  }
}
