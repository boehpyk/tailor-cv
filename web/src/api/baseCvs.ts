import { request } from './client';

import type { BaseCv, BaseCvListResponse } from '@/features/intake/types';

/**
 * Every `BaseCv` the caller's guest session owns. `items` is `[]` for a session with none — never
 * a 404 (technical-plan.md's API contract for `GET /api/base-cvs`).
 *
 * A missing, unknown or expired `tc_guest` cookie is a **401 `guest_session_expired`**, thrown as
 * an `ApiError` like any other failure — this function does not special-case it. What that 401
 * *means* for the UI (F-19: show the fresh dropzone, not an error box) is a rendering decision, not
 * a transport one, so it is made in `useBaseCvs`, one layer up, not here.
 */
export function fetchBaseCvs(signal?: AbortSignal): Promise<BaseCvListResponse> {
  return request<BaseCvListResponse>('/api/base-cvs', {
    ...(signal ? { signal } : {}),
  });
}

/**
 * Upload one base CV for the caller's guest session.
 *
 * Sent as `multipart/form-data`, one part named `file` — the name the API's `upload_base_cv`
 * handler binds via `File(...)`. `client.ts`'s `request` recognises the `FormData` body and leaves
 * `Content-Type` for the browser to set (see its docstring for why).
 *
 * Unlike the two read endpoints, the API tolerates a missing/expired cookie here by minting a
 * fresh guest session (F-17/F-18) rather than answering 401 — so this call never needs the
 * session-expiry handling `fetchBaseCvs` defers to its caller.
 */
export function uploadBaseCv(file: File, signal?: AbortSignal): Promise<BaseCv> {
  const body = new FormData();
  body.append('file', file);
  return request<BaseCv>('/api/base-cvs', {
    method: 'POST',
    body,
    ...(signal ? { signal } : {}),
  });
}

/**
 * Copy one of the signed-in user's **saved** CVs into this browser's workspace (slice 2.2): **201**
 * with the new guest `BaseCv`, `origin: 'copied_from_saved'`, extraction already done.
 *
 * **The one call that carries both credentials** — the transfer route (ADR-0008 amendment (f)).
 * `auth: 'required'` sends the bearer, which authorizes the *source* (it must be this account's
 * saved CV); `credentials: 'include'`, which every request already sends, carries the `tc_guest`
 * cookie that names the *destination* workspace. A missing or expired cookie is forgiven by the
 * server minting a new session, as 1.1's upload does. Every other guest route stays bearer-free
 * (AC-43).
 *
 * The result is a **copy**, not a link: it is guest data, deleted with the workspace after 24 hours,
 * while the saved CV stays in the account (AC-45). Not idempotent — two calls, two copies (S-36), so
 * the caller guards a double click.
 *
 * Refusals: 401 `invalid_access_token` / `not_signed_in`; 404 `base_cv_not_found` (not this
 * account's, or deleted); 409 `base_cv_not_extracted` / `too_many_base_cvs`; 410
 * `saved_base_cv_file_gone`; 422 `validation_error`; 429 `rate_limited`; 503 `storage_unavailable` /
 * `service_unavailable`.
 */
export function copySavedBaseCv(savedBaseCvId: string, signal?: AbortSignal): Promise<BaseCv> {
  return request<BaseCv>('/api/base-cvs/copies', {
    method: 'POST',
    body: { saved_base_cv_id: savedBaseCvId },
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}
