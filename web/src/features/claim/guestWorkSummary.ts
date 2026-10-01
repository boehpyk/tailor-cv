/* eslint-disable @typescript-eslint/no-unused-vars -- T29 SKELETON: the parameters are the signature qa's T30 tests compile against; T31 uses them and deletes this line. */
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
 * `null` iff no CV is left after that and there are no runs.
 *
 * SKELETON (T29): returns a sentinel that is never a correct answer (a negative count, and non-null
 * even for empty lists); T31 implements it.
 */
export function summarizeGuestWork(
  _cvs: readonly BaseCv[],
  _runs: readonly TailoringRunSummary[],
): GuestWorkSummary | null {
  return { cvFilenames: [], runCount: -1 };
}
