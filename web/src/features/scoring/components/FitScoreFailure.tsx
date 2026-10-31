import type { ErrorHold } from '@/features/retry/useHold';
import type { ScopeMap } from '@/features/scope/scopeMap';

import type { FitScoreFailureView } from '../fitScoreView';

export interface FitScoreFailureProps {
  readonly view: FitScoreFailureView;
  /** Picks the limit and busy sentences: a guest is offered `/register`, an account is not. */
  readonly scope: ScopeMap['kind'];
  /** The 429 hold, for `held`'s sentence and `HoldNote`. */
  readonly hold: ErrorHold;
  /** *Try again* on `failed`: a new, paid request, made only on this click. */
  readonly onRetry: () => void;
}

/**
 * A request that finished without a score (presentational): `failed`, `refused`, `limit`, `busy`
 * or `held`, each its own sentence. Only `failed` offers *Try again*; `held` disables nothing on its
 * own and retries nothing automatically.
 *
 * SKELETON (T35): a stub per view. T39 renders them against T38's tests.
 */
export function FitScoreFailure({ view }: FitScoreFailureProps): React.JSX.Element {
  return <div data-fit-score-view={view.kind} />;
}
