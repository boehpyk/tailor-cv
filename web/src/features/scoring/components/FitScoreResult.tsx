import type { ScopeMap } from '@/features/scope/scopeMap';

import {
  ACCOUNT_RETENTION,
  ESTIMATE_LABEL,
  GAPS_HEADING,
  GUEST_RETENTION,
  NO_CLEAR_GAP,
  scorePercent,
} from '../fitScoreCopy';

import type { FitScore } from '../types';

export interface FitScoreResultProps {
  readonly score: FitScore;
  /** Picks the retention sentence (OQ-15): a guest's score goes in 24 hours, an account's is kept. */
  readonly scope: ScopeMap['kind'];
}

/**
 * A stored score (presentational): *62%* with *estimate* beside it, the gaps as a `<ul>`, and the
 * retention sentence. Gaps are model text, rendered as React text only, never as HTML.
 *
 * The number is plain slate (AC-49): no colour, no meter, no adjective. A meter would need a
 * dynamic width, which is a `style=`.
 */
export function FitScoreResult({ score, scope }: FitScoreResultProps): React.JSX.Element {
  return (
    <div className="space-y-4">
      <p className="flex items-baseline gap-2">
        <span className="text-4xl font-semibold tabular-nums text-slate-900">
          {scorePercent(score.score_percent)}
        </span>
        <span className="text-sm text-slate-600">{ESTIMATE_LABEL}</span>
      </p>
      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-slate-900">{GAPS_HEADING}</h3>
        {score.gaps.length === 0 ? (
          <p className="text-sm text-slate-700">{NO_CLEAR_GAP}</p>
        ) : (
          <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">
            {score.gaps.map((gap) => (
              // Unique: the API refuses duplicate gaps (`gaps_inconsistent`).
              <li key={gap}>{gap}</li>
            ))}
          </ul>
        )}
      </div>
      <p className="text-sm text-slate-500">
        {scope === 'account' ? ACCOUNT_RETENTION : GUEST_RETENTION}
      </p>
    </div>
  );
}
