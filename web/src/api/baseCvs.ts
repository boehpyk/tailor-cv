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
