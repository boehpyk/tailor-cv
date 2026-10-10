import { request } from './client';

/**
 * The admin probe — `GET /api/admin/access` (slice 4.1, technical plan §4). Transport only.
 *
 * Resolves (204, no body) for an admin. Every refusal is an `ApiError`:
 * - **404** for a signed-in non-admin. The body is Starlette's `{"detail":"Not Found"}`, not the
 *   app's envelope — byte-identical to an unmatched path on purpose — so `code` is `null` and the
 *   caller branches on `status`, the one thing the firewall promises;
 * - 401 `invalid_access_token` (after `client.ts`'s one refresh-and-retry) / `not_signed_in`;
 * - 503 `service_unavailable`.
 */
export async function adminAccess(signal?: AbortSignal): Promise<void> {
  await request<null>('/api/admin/access', {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}
