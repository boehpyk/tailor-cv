import type { JobPostingSummary } from './types';

/**
 * The most recently captured posting, or `null` for a session with none.
 *
 * Compared by `created_at` rather than array position: the list endpoint's ordering is not part of
 * its contract, so reading `items[0]` as "latest" would be trusting a guarantee the server never
 * made.
 *
 * Shared by `JobPostingPanel` (which posting is on screen) and `WorkspacePage` (which posting to
 * tailor against — 1.3's `TailorPanel` until 1.4 folded the launch into the workspace) for the
 * reason `latestBaseCv`'s docstring gives: one rule, so the two readers can never disagree about
 * which posting is "the" posting.
 */
export function latestPosting(items: readonly JobPostingSummary[]): JobPostingSummary | null {
  if (items.length === 0) {
    return null;
  }
  return items.reduce((latest, item) => (item.created_at > latest.created_at ? item : latest));
}
