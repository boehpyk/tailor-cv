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

/** Which half of a run's comparison (slice 5.2): its base CV, or its tailored CV as drafted. */
export type FitScoreSide = 'before' | 'after';

/** Computed by the server's domain: *about the same* is a change of at most 5 points. */
export type ComparisonVerdict = 'higher' | 'about_the_same' | 'lower';

export interface ScoreComparison {
  /** `after − before`, in points. */
  readonly delta: number;
  readonly verdict: ComparisonVerdict;
}

/** Why a run can have no comparison at all. */
export type ComparisonUnavailableReason = 'base_cv_deleted' | 'not_succeeded';

/**
 * `GET /api/me/tailoring-runs/{id}/fit-scores` (slice 5.2, plan §4). Stored scores only: a read
 * never reaches the scorer.
 */
export interface RunFitScores {
  readonly before: FitScore | null;
  readonly after: FitScore | null;
  /** `null` unless both exist; `false` when they came from different scorer versions. */
  readonly comparable: boolean | null;
  /** `null` unless both exist and are comparable. */
  readonly comparison: ScoreComparison | null;
  readonly unavailable: ComparisonUnavailableReason | null;
}
