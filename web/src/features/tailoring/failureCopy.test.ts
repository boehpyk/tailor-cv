import { describe, expect, it } from 'vitest';

import { failureCopyFor } from './failureCopy';

import type { TailoringFailureReason } from './types';

/**
 * T44 — a **runtime** check that `failureCopyFor` has an entry for every member of
 * `TailoringFailureReason`, complementing the type-level exhaustiveness `Record<TailoringFailureReason,
 * …>` already gives at compile time (task-list T44; `failureCopy.ts`'s own docblock names this
 * property). The type-level check catches a *removed* member changing shape; it cannot catch a typo
 * in a literal key, because `Record<Union, …>` still requires the exact literal spelling to satisfy
 * the type — a test is what actually calls the function with each value and checks something came
 * back, the way a user's browser would.
 *
 * This list is maintained by hand against `types.ts`'s union — the same "hand-written for now" seam
 * that file's own docblock names — and is deliberately **not** copied from `TailorPanel.test.tsx`'s
 * `FAILURE_REASON_CASES`, which covers only the six reasons AC-13 examines the retry button for.
 * `not_queued` and `abandoned` are the two this file adds coverage for.
 */
const ALL_FAILURE_REASONS: readonly TailoringFailureReason[] = [
  'llm_unavailable',
  'llm_rate_limited',
  'llm_refused',
  'llm_timed_out',
  'llm_output_invalid',
  'inputs_too_large',
  'llm_error',
  'not_queued',
  'abandoned',
];

describe('failureCopyFor', () => {
  it.each(ALL_FAILURE_REASONS)('has a non-empty headline for %s', (reason) => {
    const copy = failureCopyFor(reason);

    expect(copy.headline.length).toBeGreaterThan(0);
  });

  it('gives every reason a distinct headline', () => {
    // Not a spec requirement, but a copy-paste in a nine-entry map is exactly the kind of mistake
    // this runtime check exists to catch that the type checker cannot: two reasons sharing a
    // headline would tell two different failures apart from nothing, in a table meant to keep them
    // apart.
    const headlines = ALL_FAILURE_REASONS.map((reason) => failureCopyFor(reason).headline);
    expect(new Set(headlines).size).toBe(headlines.length);
  });

  it('falls back to a non-empty headline for a null reason', () => {
    const copy = failureCopyFor(null);

    expect(copy.headline.length).toBeGreaterThan(0);
  });
});
