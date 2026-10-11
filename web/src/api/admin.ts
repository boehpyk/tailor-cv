import { request } from './client';

import type { Role } from '@/features/auth/types';

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

// --- The user directory (slice 4.2, technical plan §4) --------------------------------------------
//
// Every call below is bearer-only behind the same firewall as the probe, so a non-admin's refusal
// is the same code-less 404, and a disabled actor's is 403 `account_disabled`. Every 200 is
// `no-store` on the server's side; the client keeps what it reads only in the query cache, under
// the actor's account key root, so a sign-out drops every email an admin loaded (AC-38).

/** The account directory's root. */
const USERS_PATH = '/api/admin/users';

/**
 * One row of the directory. `created_at`/`disabled_at` are ISO-8601 with an offset, whole seconds;
 * `disabled_at` is `null` for an active account.
 */
export interface AdminUserSummary {
  readonly id: string;
  readonly email: string;
  readonly role: Role;
  readonly created_at: string;
  readonly disabled_at: string | null;
}

/** One page, newest first. `next_cursor` is opaque, passed back verbatim; `null` on the last page. */
export interface UserDirectoryPage {
  readonly items: readonly AdminUserSummary[];
  readonly next_cursor: string | null;
}

/** What an account holds — the same counts `erase-account --dry-run` prints. */
export interface AccountCounts {
  readonly base_cvs: number;
  readonly files: number;
  readonly logins: number;
  readonly tailoring_runs: number;
  readonly job_postings: number;
  readonly export_jobs: number;
  readonly tracked_applications: number;
}

export interface AdminUserDetail extends AdminUserSummary {
  readonly counts: AccountCounts;
}

/** The answer to a disable or an enable. `changed` is `false` when it already was. */
export interface AccountStatusChange {
  readonly user_id: string;
  readonly disabled_at: string | null;
  readonly changed: boolean;
  readonly logins_revoked: number;
}

/** What an erase removed. `unlink_failures > 0` leaves files for the orphan sweep (OQ-11). */
export interface AccountErasureReport {
  readonly base_cvs: number;
  readonly files: number;
  readonly files_unlinked: number;
  readonly unlink_failures: number;
  readonly tailoring_runs: number;
  readonly job_postings: number;
  readonly export_jobs: number;
  readonly tracked_applications: number;
}

function userPath(userId: string): string {
  return `${USERS_PATH}/${encodeURIComponent(userId)}`;
}

/**
 * One page of the directory. `q` is the email search (`''` = everyone; sent only when non-empty),
 * `cursor` is `null` for the first page. Refusals: 422 `invalid_search` / `invalid_cursor` /
 * `validation_error`.
 */
export function listUsers(
  q: string,
  cursor: string | null,
  signal?: AbortSignal,
): Promise<UserDirectoryPage> {
  const query = new URLSearchParams();
  if (q !== '') query.set('q', q);
  if (cursor !== null) query.set('cursor', cursor);
  const search = query.toString();
  return request<UserDirectoryPage>(search === '' ? USERS_PATH : `${USERS_PATH}?${search}`, {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/** One account and its counts. 404 `user_not_found` when it does not exist (or was erased). */
export function getUser(userId: string, signal?: AbortSignal): Promise<AdminUserDetail> {
  return request<AdminUserDetail>(userPath(userId), {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/** Disable an account and revoke its logins. Idempotent (`changed: false`). 404 `user_not_found`. */
export function disableUser(userId: string): Promise<AccountStatusChange> {
  return request<AccountStatusChange>(`${userPath(userId)}/disable`, {
    method: 'POST',
    auth: 'required',
  });
}

/** Enable an account (`logins_revoked: 0`). Idempotent. 404 `user_not_found`. */
export function enableUser(userId: string): Promise<AccountStatusChange> {
  return request<AccountStatusChange>(`${userPath(userId)}/enable`, {
    method: 'POST',
    auth: 'required',
  });
}

/** Erase an account: rows committed, then files. Irreversible. 404 `user_not_found`. */
export function eraseUser(userId: string): Promise<AccountErasureReport> {
  return request<AccountErasureReport>(userPath(userId), {
    method: 'DELETE',
    auth: 'required',
  });
}
