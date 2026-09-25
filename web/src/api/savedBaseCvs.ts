import { request } from './client';

import type { SavedBaseCv, SavedBaseCvList } from '@/features/savedCvs/types';

/**
 * The four `/api/me/base-cvs` endpoints (slice 2.2, technical plan §4) — transport only.
 *
 * **Every call here is `auth: 'required'`** (AC-43). These are the account's CVs, authorized by the
 * bearer and by nothing else: the server ignores a `tc_guest` cookie on these routes (AC-21), and
 * the client sends the token because the route needs it, never because one happens to be held.
 * `api/client.ts` attaches it, refreshes first when it is near expiry, and answers one 401
 * `invalid_access_token` with one refresh and one retry; a 401 `not_signed_in` (the account is gone)
 * tells the store and is re-thrown.
 *
 * **Why `/api/me/…`.** The path says whose resource it is. `/api/base-cvs` stays "this browser's
 * workspace", `/api/me/base-cvs` is "my account's CVs" — two owners, two credentials, two paths.
 *
 * Every refusal is an `ApiError`; callers branch on its `code`, never on its `message`.
 */

/** The path of one saved CV. The id came from the server, but it is still encoded, not trusted. */
function savedBaseCvPath(id: string): string {
  return `/api/me/base-cvs/${encodeURIComponent(id)}`;
}

/**
 * Every saved CV the signed-in account keeps, newest first — `items: []` for none, never a 404.
 *
 * Refusals: 401 `invalid_access_token` / `not_signed_in`; 503 `service_unavailable`.
 */
export function fetchSavedBaseCvs(signal?: AbortSignal): Promise<SavedBaseCvList> {
  return request<SavedBaseCvList>('/api/me/base-cvs', {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * Upload one CV to the account: **201** `SavedBaseCv`, extraction already decided — `extracted`, or
 * `extraction_failed` with the server's `failure_message`. That is a success, not a refusal: the CV
 * is saved and listed, and the row says why it cannot be used (AC-35).
 *
 * Multipart, one part named `file`, exactly like 1.1's guest upload (`api/baseCvs.ts`).
 *
 * Refusals: 413 `file_too_large`; 415 `unsupported_format`; 422 `missing_file` / `empty_file` /
 * `invalid_filename`; 409 `too_many_saved_base_cvs` (the message names the cap — the client never
 * knows the number, AC-35); 429 `rate_limited` (`retryAfterSeconds`); 401 ×2; 503
 * `storage_unavailable` / `service_unavailable`.
 */
export function uploadSavedBaseCv(file: File, signal?: AbortSignal): Promise<SavedBaseCv> {
  const body = new FormData();
  body.append('file', file);
  return request<SavedBaseCv>('/api/me/base-cvs', {
    method: 'POST',
    body,
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * Set a saved CV's label, or clear it with `null`: **200** with the updated `SavedBaseCv`.
 *
 * The label is sent as typed. Trimming, the 80-character limit and the ban on control characters
 * are the server's rules (`BaseCvLabel`), refused as 422 `invalid_label`; the client does not
 * re-implement them (Constitution §4.5). Also: 404 `base_cv_not_found` (deleted meanwhile, or not
 * this account's — byte-identical); 422 `validation_error`; 401 ×2; 503 `service_unavailable`.
 */
export function renameSavedBaseCv(
  id: string,
  label: string | null,
  signal?: AbortSignal,
): Promise<SavedBaseCv> {
  return request<SavedBaseCv>(savedBaseCvPath(id), {
    method: 'PATCH',
    body: { label },
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * Delete a saved CV — its row, its file and the text read from it: **204**. Irreversible.
 *
 * A 404 `base_cv_not_found` means it is already gone (a second tab, a double submit): thrown like
 * any refusal here, and read as "already gone" one layer up, in `useDeleteSavedBaseCv` — what a
 * status *means* is not transport's call. A 503 `service_unavailable` means it was **not** deleted.
 */
export async function deleteSavedBaseCv(id: string, signal?: AbortSignal): Promise<void> {
  await request<null>(savedBaseCvPath(id), {
    method: 'DELETE',
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}
