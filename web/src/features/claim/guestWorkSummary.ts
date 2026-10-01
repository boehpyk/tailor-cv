import type { BaseCv } from '@/features/intake/types';
import type { TailoringRunSummary } from '@/features/tailoring/types';

/**
 * What the claim offer names (AC-37): this browser's guest CVs by filename, and how many tailored
 * applications it holds. An **estimate** read from the guest lists before the claim — the claim's
 * response carries the authoritative counts (technical plan §0.11).
 */
export interface GuestWorkSummary {
  /** `original_filename` of every guest CV that is **not** a working copy, in list order. */
  readonly cvFilenames: readonly string[];
  /** How many tailoring runs the guest session holds. */
  readonly runCount: number;
}

/**
 * **Pure**: the guest lists → what the offer names, or `null` when there is nothing to offer.
 *
 * Working copies (`origin: 'copied_from_saved'`) are left out: a claim never moves one (OQ-3) — the
 * user already owns its source — so offering it would promise something the server will not do.
 * `null` iff no CV is left after that and there are no runs. Every run counts, whatever its status:
 * a failed run moves too.
 */
export function summarizeGuestWork(
  cvs: readonly BaseCv[],
  runs: readonly TailoringRunSummary[],
): GuestWorkSummary | null {
  const cvFilenames = cvs
    .filter((cv) => cv.origin !== 'copied_from_saved')
    .map((cv) => cv.original_filename);
  if (cvFilenames.length === 0 && runs.length === 0) {
    return null;
  }
  return { cvFilenames, runCount: runs.length };
}
