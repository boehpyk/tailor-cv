/**
 * Mirrors `TrackedApplicationResponse`, `BoardCardResponse` and `BoardResponse` (and the three
 * nested shapes) in the API's `infrastructure/api/schemas/tracking.py`, field for field, nullability
 * included (slice 3.1, technical plan §4).
 *
 * Hand-written — the same known seam `features/history/types.ts` names: two declarations of one
 * contract can drift, and the OpenAPI schema is where these should eventually be generated from.
 *
 * Wire types, not view models. **No rule lives here.** Which stage may follow which (any may — the
 * server decides, plan §0.4), what a title may hold (`ApplicationTitle`), which run may be tracked
 * (succeeded only): the API is the authority on each, and the client reads its answers (Constitution
 * §4.5). `STAGES`' order is presentation — the board's left-to-right — not a workflow.
 */

import type { PostingSource } from '@/features/posting/types';

/**
 * The six stages, in the order the board shows its columns. A closed set (plan §0.3), mirroring the
 * domain's `ApplicationStage`; the wire values are these strings exactly.
 */
export const STAGES = [
  'to_apply',
  'applied',
  'interviewing',
  'offer',
  'rejected',
  'withdrawn',
] as const;

export type Stage = (typeof STAGES)[number];

const STAGE_SET: ReadonlySet<string> = new Set(STAGES);

/** Narrow a string (a `<select>`'s value, a drop payload) to a `Stage` without a cast. */
export function isStage(value: string): value is Stage {
  return STAGE_SET.has(value);
}

/**
 * A card as a write returns it — `POST` 201, both `PUT`s 200: exactly these seven keys (AC-21).
 * `version` is what the next move or retitle sends back (ADR-0015's optimistic concurrency).
 */
export interface TrackedApplication {
  readonly id: string;
  readonly tailoring_run_id: string;
  readonly stage: Stage;
  /** The user's own label, or `null` — the card then shows its posting's title, else its preview. */
  readonly title: string | null;
  readonly tracked_at: string;
  readonly stage_changed_at: string;
  readonly version: number;
}

/** The run a card refers to: when it was requested, and whether its documents carry an edit. */
export interface BoardRun {
  readonly requested_at: string;
  readonly edited: boolean;
}

/**
 * The posting a card's run was tailored to. `preview` is at most 140 characters, cut in SQL — the
 * full text is never on the board (AC-31). `source_url` is rendered only as an `http(s)` link
 * (AC-40), never fetched.
 */
export interface BoardPosting {
  readonly job_posting_id: string;
  readonly source: PostingSource;
  readonly title: string | null;
  readonly source_url: string | null;
  readonly preview: string;
}

/** The saved CV a card's run started from; `null` on the card once that CV is deleted (T-25). */
export interface BoardBaseCv {
  readonly base_cv_id: string;
  readonly label: string | null;
  readonly original_filename: string;
}

/**
 * One card on the board: the card's seven fields plus three nullable joins (AC-30).
 *
 * **Not declared as `TrackedApplication & {…}`**, for the API's reason (schemas/tracking.py, point
 * 4): one is what a write returns, the other a row of a read model, and an intersection would make
 * the next field on either appear in both by default. `run` is `null` (and `posting` with it) only
 * for a card whose run row is missing — prevented by the server's locks, still handled (T-36).
 */
export interface BoardCard {
  readonly id: string;
  readonly tailoring_run_id: string;
  readonly stage: Stage;
  readonly title: string | null;
  readonly tracked_at: string;
  readonly stage_changed_at: string;
  readonly version: number;
  readonly run: BoardRun | null;
  readonly posting: BoardPosting | null;
  readonly base_cv: BoardBaseCv | null;
}

/**
 * `GET /api/me/board`: every card, `stage_changed_at DESC, id DESC`, unpaginated (ADR-0024
 * amendment (a)). An empty board is `{ items: [] }`, a 200 and never a 404.
 */
export interface Board {
  readonly items: readonly BoardCard[];
}

/** What `POST /api/me/tracked-applications` sends. `stage` defaults to `to_apply` server-side. */
export interface TrackApplicationInput {
  readonly tailoring_run_id: string;
  readonly stage?: Stage;
  readonly title?: string | null;
}

/**
 * Every `code` a tracking endpoint can answer with (technical plan §4's table, plus the common three
 * every `/api/me/` route shares).
 *
 * Closed *for these endpoints*, which is what lets `trackingCopy.ts` be exhaustive. `ApiError.code`
 * stays `string | null` — the client cannot promise the server never grows a code — so narrow with
 * `isTrackingErrorCode` at the point of use rather than casting.
 *
 * Two codes carry a fact in `ApiError.details`: `application_already_tracked` names the existing
 * card (`tracked_application_id`), and `tracked_application_version_conflict` the card's
 * `current_version` (`null` when the card was deleted mid-request, T-21).
 */
export const TRACKING_ERROR_CODES = [
  'tailoring_run_not_found',
  'tailoring_run_not_trackable',
  'application_already_tracked',
  'too_many_tracked_applications',
  'tracked_application_not_found',
  'tracked_application_version_conflict',
  'validation_error',
  'rate_limited',
  'invalid_access_token',
  'not_signed_in',
  'service_unavailable',
] as const;

export type TrackingErrorCode = (typeof TRACKING_ERROR_CODES)[number];

const TRACKING_ERROR_CODE_SET: ReadonlySet<string> = new Set(TRACKING_ERROR_CODES);

export function isTrackingErrorCode(code: string | null): code is TrackingErrorCode {
  return code !== null && TRACKING_ERROR_CODE_SET.has(code);
}
