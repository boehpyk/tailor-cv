import { GUEST_SCOPE_MAP } from '@/features/scope/scopeMap';

import type { FitScorePair } from '../types';
import type { ScopeMap } from '@/features/scope/scopeMap';

/**
 * The cache key for one pair's score: `[...keyRoot, 'scoring', 'fitScore', baseCvId, postingId]`.
 * A guest's starts with `'scoring'`, which is why `GUEST_QUERY_ROOTS` lists it: a claim must forget
 * it. An account's sits under `['auth', 'account', userId]`, so a guest and a user never share an
 * entry (AC-44). `null` is the disabled query's key, as in `tailoringRunQueryKey`.
 */
export function fitScoreQueryKey(
  pair: FitScorePair | null,
  map: Pick<ScopeMap, 'keyRoot'> = GUEST_SCOPE_MAP,
): readonly unknown[] {
  return [
    ...map.keyRoot,
    'scoring',
    'fitScore',
    pair?.base_cv_id ?? null,
    pair?.job_posting_id ?? null,
  ];
}
