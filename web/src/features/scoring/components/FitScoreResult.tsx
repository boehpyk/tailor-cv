import type { ScopeMap } from '@/features/scope/scopeMap';

import type { FitScore } from '../types';

export interface FitScoreResultProps {
  readonly score: FitScore;
  /** Picks the retention sentence (OQ-15): a guest's score goes in 24 hours, an account's is kept. */
  readonly scope: ScopeMap['kind'];
}

/**
 * A stored score (presentational): *62%* with *estimate* beside it, the gaps as a `<ul>`, and the
 * retention sentence. Gaps are model text, rendered as text only, never as HTML.
 *
 * SKELETON (T35): a stub. T39 renders it against T38's tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T35 skeleton; read in T39
export function FitScoreResult(_props: FitScoreResultProps): React.JSX.Element {
  return <div data-fit-score-view="succeeded" />;
}
