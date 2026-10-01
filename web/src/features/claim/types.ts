/**
 * Mirrors `GuestWorkClaimResponse` in the API's `infrastructure/api/schemas/guest_work.py`, field
 * for field (slice 2.4, technical plan §4). Hand-written, like every other mirror here.
 *
 * **Counts only, and all of them the server's.** What moved is decided in one transaction on the
 * server (ADR-0025); the client never adds up its own guest lists to say what was kept — those were
 * an *estimate* for the offer, read before the claim, and this is the answer. **All zeros is a
 * success**: "nothing left to claim" (no cookie, an expired session, a claim whose first response was
 * lost) answers 200, so a retry never reads as a failure.
 *
 * `working_copies_dropped` counts 2.2's working copies, which a claim never moves (OQ-3): the user
 * already owns their source, so they stay with the guest session and go with it.
 */
export interface GuestWorkClaimResult {
  readonly base_cvs: number;
  readonly job_postings: number;
  readonly tailoring_runs: number;
  readonly export_jobs: number;
  readonly working_copies_dropped: number;
}
