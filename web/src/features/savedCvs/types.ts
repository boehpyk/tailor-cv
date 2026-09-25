/**
 * The wire shapes of `/api/me/base-cvs` (slice 2.2, technical plan §4), mirrored by hand from
 * `SavedBaseCvResponse` / `SavedBaseCvListResponse` in the API's `schemas/intake.py`, field for field.
 *
 * **A saved CV is not a guest `BaseCv` with an extra field**, even though the two share most of a
 * shape, and neither type extends the other (CLAUDE.md: shared shape is not shared behaviour):
 *
 * - **No `expires_at`.** A saved CV never expires — it stays until its owner deletes it. A field that
 *   could only ever be `null` here would invite a component to render "expires in …" for it.
 * - **A `label`** the user chose (or `null`), which a guest CV cannot have — the database refuses a
 *   label on a guest row (`ck_intake_base_cv_label_only_when_user_owned`).
 * - **No `origin`.** Only a guest CV can be a working copy; a saved CV is always one the user uploaded.
 *
 * Like `BaseCv`, it carries **no text and no bytes** — `character_count` is computed by Postgres
 * without the text ever being selected (the read model, technical plan §3 amendment).
 */

import type { BaseCvStatus, CvContentType, ExtractionFailureReason } from '@/features/intake/types';

/** One saved CV, as `GET`/`POST /api/me/base-cvs` and `PATCH /api/me/base-cvs/{id}` describe it. */
export interface SavedBaseCv {
  readonly id: string;
  /** The user's own name for it, or `null` — then the UI shows `original_filename` instead. */
  readonly label: string | null;
  readonly original_filename: string;
  readonly content_type: CvContentType;
  readonly size_bytes: number;
  readonly status: BaseCvStatus;
  readonly character_count: number | null;
  readonly failure_reason: ExtractionFailureReason | null;
  /** The server's sentence for `failure_reason`; the client never re-derives it (AC-35). */
  readonly failure_message: string | null;
  /** ISO-8601, UTC. The list is newest first by this — the server's order, never re-sorted here. */
  readonly uploaded_at: string;
}

/** `GET /api/me/base-cvs`: every saved CV the account keeps, newest first; `[]` for none. */
export interface SavedBaseCvList {
  readonly items: readonly SavedBaseCv[];
}
