import { request } from './client';

import type { GuestWorkClaimResult } from '@/features/claim/types';

/**
 * The claim — `POST /api/me/guest-work/claim` (slice 2.4, technical plan §4, ADR-0025). Transport
 * only.
 *
 * **Two credentials, one request, each authorizing its own half** (ADR-0008 amendment (g)). The
 * bearer names the destination — the account the work moves into — so the call is
 * `auth: 'required'`, with `api/client.ts`'s refresh-once-and-retry on 401 `invalid_access_token`.
 * The source is the `__Host-tc_guest` cookie, which travels because every request here is
 * same-origin with `credentials: 'include'`, exactly as it does today; nothing here reads, sets or
 * names it. That is the one route where carrying both is the design rather than the accident
 * `RequestOptions.auth` warns about.
 *
 * **No body.** The server ignores one; which work moves is "everything the cookie's session owns",
 * never a client-chosen list.
 *
 * **Safe to retry** (C-39, C-40): after a claim the session is gone, so a second call presents a
 * cookie that names nothing and answers 200 with zeros.
 *
 * Refusals (each an `ApiError`, branch on `code`): 401 `invalid_access_token` / `not_signed_in`;
 * 429 `rate_limited` (`retryAfterSeconds`); 503 `service_unavailable`. None of them moved anything.
 */
export function claimGuestWork(signal?: AbortSignal): Promise<GuestWorkClaimResult> {
  return request<GuestWorkClaimResult>('/api/me/guest-work/claim', {
    method: 'POST',
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}
