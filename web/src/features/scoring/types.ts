/**
 * Mirrors `api/src/tailorcraft/infrastructure/api/schemas/scoring.py`, field for field (slice 5.1,
 * technical plan §4). Hand-written, like every other mirror here.
 *
 * **What is absent is the contract:** no fingerprint, token count, cost or model name. The client
 * never sees them, so nothing here can display or log them.
 */

/** The pair a score is about: the same two ids a tailoring run is launched from (`launchInput`). */
export interface FitScorePair {
  readonly base_cv_id: string;
  readonly job_posting_id: string;
}

/** One stored score. `expires_at` is the guest session's expiry; `null` for an account. */
export interface FitScore {
  readonly id: string;
  /** 0–100, an **estimate** (OQ-16). Never a probability of being hired. */
  readonly score_percent: number;
  /** 0–3 requirements the posting asks for that the CV doesn't show. Untrusted model text. */
  readonly gaps: readonly string[];
  readonly scored_at: string;
  readonly expires_at: string | null;
}

/**
 * `GET`'s answer. `null` is the ordinary "not scored yet", not an error, and a 200 — TanStack v5
 * refuses `undefined` data, so emptiness has to be a value.
 */
export interface FitScoreForPair {
  readonly fit_score: FitScore | null;
}
