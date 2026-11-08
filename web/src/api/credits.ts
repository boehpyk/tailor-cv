import { request } from './client';

import type { Credits, PackId, PaymentNoticeState } from '@/features/billing/types';

/**
 * The credit endpoints (slice 5.2, technical plan §4). **Bearer-only**, every one: a credit belongs
 * to a user, so there is no guest twin and no `ApiTarget`. Every answer is `no-store` on the
 * server's side; the client keeps it only in the query cache under the account's key root.
 *
 * Rejections are `ApiError`s: 401 `not_signed_in` / `invalid_access_token`, 403 `account_disabled`,
 * 503 `service_unavailable`, and a 422 `validation_error` for a body the schema refuses.
 */

/** The balance, the notice, the packs and the newest 100 entries. A read: safe to retry. */
export function fetchCredits(signal?: AbortSignal): Promise<Credits> {
  return request<Credits>('/api/me/credits', {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * An account chooses a pack. **Charges nothing** (AC-25): it counts the click and answers whether
 * the account has opted in to the payment notice, so the dialog can show the checkbox's state.
 */
export function postPackClick(pack: PackId): Promise<PaymentNoticeState> {
  return request<PaymentNoticeState>('/api/me/pack-clicks', {
    method: 'POST',
    auth: 'required',
    body: { pack },
  });
}

/** Record or clear consent to one plain-text email when payments open (AC-26). Idempotent. */
export function putPaymentNotice(optedIn: boolean): Promise<PaymentNoticeState> {
  return request<PaymentNoticeState>('/api/me/payment-notice', {
    method: 'PUT',
    auth: 'required',
    body: { opted_in: optedIn },
  });
}
