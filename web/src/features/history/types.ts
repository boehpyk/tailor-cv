/**
 * Mirrors `HistoryEntryResponse` / `HistoryPageResponse` (and the two nested shapes) in the API's
 * `infrastructure/api/schemas/tailoring.py`, field for field, nullability included (slice 2.3,
 * technical plan §4, ADR-0024).
 *
 * Hand-written, the same known seam `features/tailoring/types.ts` names: two declarations of one
 * contract drift, and the OpenAPI schema is where these should eventually be generated from.
 *
 * Wire types, not view models. **No rule lives here** — which entry may be deleted, what "CV
 * deleted" means, whether *Try again* is offered: those are the API's (`retryable`, a `DELETE`'s
 * 409) or the view's copy, never a TypeScript re-derivation (Constitution §4.5).
 */

import type { PostingSource } from '@/features/posting/types';
import type { TailoringFailureReason, TailoringRunStatus } from '@/features/tailoring/types';

/** The saved CV an entry was tailored from, present only while that saved CV still exists. */
export interface HistoryBaseCv {
  readonly id: string;
  readonly label: string | null;
  readonly original_filename: string;
}

/**
 * The job posting an entry was tailored against. `preview` is at most 140 characters, cut in SQL —
 * the full text is never in a list (AC-55).
 */
export interface HistoryPosting {
  readonly id: string;
  readonly source: PostingSource;
  readonly title: string | null;
  readonly source_url: string | null;
  readonly preview: string;
}

/**
 * One run in a signed-in user's history: how it ended and what is left of its two inputs — **never
 * a document body** (AC-55). The documents are one request away, on the run itself.
 *
 * **Not a `TailoringRunSummary`**, and not derived from one: the guest list is 1.3's shape for a
 * 24-hour workspace; this one carries what a user needs to recognise an entry a month later.
 * Shared shape is not shared meaning.
 *
 * `base_cv_id` and `base_cv` are **two facts, two fields**: the id is the run's own reference and is
 * always there; `base_cv` is `null` exactly when that saved CV has since been deleted — the derived
 * *"CV deleted"* state (H-29), computed at read time and never stored. `posting` is `null` only for
 * an entry whose posting row is missing, which should be impossible (H-30) — handled, not asserted.
 */
export interface HistoryEntry {
  readonly id: string;
  readonly status: TailoringRunStatus;
  readonly failure_reason: TailoringFailureReason | null;
  /** See `TailoringRun.retry_not_before`. */
  readonly retry_not_before: string | null;
  /** Computed by the API — see `TailoringRun.retryable`. Read it; never re-derive it. */
  readonly retryable: boolean;
  readonly requested_at: string;
  readonly completed_at: string | null;
  readonly version: number;
  /** Whether either document carries the user's own revision (the *Edited* badge). */
  readonly edited: boolean;
  readonly base_cv_id: string;
  readonly base_cv: HistoryBaseCv | null;
  readonly posting: HistoryPosting | null;
}

/**
 * One keyset page of history, newest first (ADR-0024). `next_cursor` is **opaque** — sent back
 * verbatim as `?cursor=`, never parsed, built or compared here — and `null` on the last page. An
 * empty history is `{ items: [], next_cursor: null }`, a 200 and never a 404.
 */
export interface HistoryPage {
  readonly items: readonly HistoryEntry[];
  readonly next_cursor: string | null;
}
