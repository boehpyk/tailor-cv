import type { TailoringRunSummary } from './types';

/**
 * The session's most recently requested run, or `null` for a session with none — **the run the
 * panel watches on load**, which is what makes a refresh reattach to a run in progress instead of
 * starting (and paying for) a new one.
 *
 * Compared by `requested_at`, not by array position, for the reason `latestBaseCv` and
 * `latestPosting` give. The comparison is strict, so on a whole-second tie the earlier list item
 * wins — and the API lists newest first, so a tie still resolves to the newer run.
 */
export function latestTailoringRun(
  items: readonly TailoringRunSummary[],
): TailoringRunSummary | null {
  if (items.length === 0) {
    return null;
  }
  return items.reduce((latest, item) => (item.requested_at > latest.requested_at ? item : latest));
}
