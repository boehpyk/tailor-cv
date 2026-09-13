/**
 * Mirrors `TailoringRunResponse` / `TailoringRunSummary` / `TailoringRunListResponse` and
 * `CreateTailoringRunRequest` in the API's `infrastructure/api/schemas/tailoring.py`, field for
 * field, nullability included.
 *
 * Hand-written for now, and that is a known seam (`features/intake/types.ts` and
 * `features/posting/types.ts` carry the same note): two declarations of one contract drift. When the
 * API surface grows past a couple of endpoints, generate these from the OpenAPI schema FastAPI
 * already publishes at `/openapi.json` rather than maintaining them by hand.
 *
 * Wire types, not view models: UUIDs are `string`, datetimes are ISO-8601 `string`s, and every
 * field is `readonly` because a response is a fact the server stated, not something the client edits.
 */

/**
 * Where a run stands. Mirrors the domain's `TailoringRunStatus` — a closed set of four, so a union
 * of literals rather than `string`: an unhandled member is a compile error, not a silent fallthrough.
 *
 * `queued` and `running` are the two non-terminal states the poller keeps polling through;
 * `succeeded` and `failed` are terminal and decided exactly once. **A `failed` run arrives as a 200**,
 * on the same polled resource as a `running` one — it is a recorded state of the run, not an HTTP
 * error, which is why the UI must tell the two apart by `status` and never by the response code.
 */
export type TailoringRunStatus = 'queued' | 'running' | 'succeeded' | 'failed';

/**
 * Why a run ended in `failed`. Mirrors the domain's `TailoringFailureReason` — nine values, closed.
 *
 * A component maps these to copy with an exhaustive `Record<TailoringFailureReason, …>`, so a tenth
 * reason added on the server becomes a TypeScript error at the one place that must write a sentence
 * for it, rather than a blank box in the browser.
 *
 * What these reasons do **not** decide here is whether "Try again" is offered. That is `retryable`,
 * below, and it comes from the API.
 */
export type TailoringFailureReason =
  | 'llm_unavailable'
  | 'llm_rate_limited'
  | 'llm_refused'
  | 'llm_timed_out'
  | 'llm_output_invalid'
  | 'inputs_too_large'
  | 'llm_error'
  | 'not_queued'
  | 'abandoned';

/**
 * The request body for `POST /api/tailoring-runs` — the whole of it.
 *
 * There is no model, prompt version, temperature or token budget, and the absence is the contract:
 * the client may ask for "tailor this CV against this posting" and nothing about how that is paid
 * for (the Pydantic model is `extra="forbid"`, so a stray field is a 422).
 */
export interface NewTailoringRun {
  readonly base_cv_id: string;
  readonly job_posting_id: string;
}

/**
 * One run in full, as `POST /api/tailoring-runs` (202) and `GET /api/tailoring-runs/{id}` (200)
 * return it — including both tailored documents.
 *
 * Every field from `tailored_cv` down to `llm_duration_ms`, plus `started_at` / `completed_at`, is
 * `null` while the run is `queued` or `running`. That is the polling contract: the client re-reads
 * this same shape until `status` is terminal.
 *
 * `prompt_tokens` and `completion_tokens` are deliberately absent on the server — cost accounting,
 * nothing a user can act on — so they are absent here too. Do not "complete" this type from the
 * database columns.
 */
export interface TailoringRun {
  readonly id: string;
  readonly status: TailoringRunStatus;
  readonly base_cv_id: string;
  readonly job_posting_id: string;

  readonly failure_reason: TailoringFailureReason | null;

  /**
   * Whether "Try again" is worth offering. **Computed by the API from `failure_reason`; the client
   * reads it and never re-derives it** (AC-13, Constitution §4.5). "Which failures are worth paying
   * for again" is a business rule, and a TypeScript copy of it would be a second authority that
   * drifts. `false` for a run that has not failed, which is not a claim that it is unretryable —
   * read it only when `status === 'failed'`.
   */
  readonly retryable: boolean;

  /** Untrusted model output. Render it as text, never as HTML (AC-31). */
  readonly tailored_cv: string | null;
  /** Untrusted model output. Render it as text, never as HTML (AC-31). */
  readonly cover_letter: string | null;
  readonly tailored_cv_character_count: number | null;
  readonly cover_letter_character_count: number | null;

  /** Provenance: what actually produced these words. `null` until the call succeeds. */
  readonly model: string | null;
  readonly prompt_version: string | null;
  readonly llm_duration_ms: number | null;

  readonly requested_at: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;

  /** The **guest session's** expiry — the session owns the 24-hour promise (ADR-0006). */
  readonly expires_at: string;
}

/**
 * One run as it appears in a list: `TailoringRun` **minus the two document bodies**.
 *
 * Written out rather than `Omit<TailoringRun, 'tailored_cv' | 'cover_letter'>`, for the reason the
 * server's `TailoringRunSummary` is not a subclass of `TailoringRunResponse`: a derivation makes the
 * *next* field added to the full shape appear in the list type by default, and the direction that
 * default leaks is a stranger's rewritten CV. Here it would only be a type that lies about the wire,
 * but a type that lies is how a component comes to read a field that is never there.
 */
export interface TailoringRunSummary {
  readonly id: string;
  readonly status: TailoringRunStatus;
  readonly base_cv_id: string;
  readonly job_posting_id: string;

  readonly failure_reason: TailoringFailureReason | null;
  /** Computed by the API — see `TailoringRun.retryable`. */
  readonly retryable: boolean;

  readonly tailored_cv_character_count: number | null;
  readonly cover_letter_character_count: number | null;

  readonly model: string | null;
  readonly prompt_version: string | null;
  readonly llm_duration_ms: number | null;

  readonly requested_at: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly expires_at: string;
}

/** Every run a guest session owns, newest first. `items` is `[]` for a session with none. */
export interface TailoringRunListResponse {
  readonly items: readonly TailoringRunSummary[];
}

/**
 * Whether a run is still in flight — `queued` or `running` — and so worth polling.
 *
 * This restates the server's own documented polling contract (the two non-terminal states of
 * `TailoringRunStatus`), not a business rule the client invents. It lives here, once, so the poller
 * and the panel that decides "is this the working state" cannot disagree about which statuses count.
 * Written as a `switch` over the union so a fifth status is a compile error rather than a run the
 * poller silently abandons.
 */
export function isActiveTailoringRunStatus(status: TailoringRunStatus): boolean {
  switch (status) {
    case 'queued':
    case 'running':
      return true;
    case 'succeeded':
    case 'failed':
      return false;
  }
}
