import { bearerFor, ok } from './accountFetch';
import { jsonResponse } from './fixtures';

import type { RouteHandler } from './accountFetch';
import type { AccountCounts, AdminUserDetail, AdminUserSummary } from '@/api/admin';
import type { User } from '@/features/auth/types';

/**
 * Slice 4.2 test support (T20): fixtures and a firewall for `/api/admin/users*`.
 *
 * **The fake enforces who is asking** (2.4's `ed66fbe`): `adminOnly` answers the firewall's code-less
 * 404 to any bearer but the admin's, so "a plain user never sees the directory" fails against a
 * client that sends the wrong credential instead of passing against a fake that answers anyone.
 */

export const firewallNotFound = (): Response =>
  new Response(JSON.stringify({ detail: 'Not Found' }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' },
  });

export const noContent = (): Response => new Response(null, { status: 204 });

export function adminOnly(admin: User, handler: RouteHandler): RouteHandler {
  return (call, n) =>
    call.authorization === bearerFor(admin) ? handler(call, n) : firewallNotFound();
}

export function summary(overrides: Partial<AdminUserSummary> = {}): AdminUserSummary {
  return {
    id: 'u-1',
    email: 'ann@example.com',
    role: 'user',
    created_at: '2026-09-01T10:00:00Z',
    disabled_at: null,
    ...overrides,
  };
}

/** Seven different numbers, so a count wired to the wrong label fails. */
export const COUNTS: AccountCounts = {
  base_cvs: 1,
  files: 2,
  logins: 3,
  tailoring_runs: 4,
  job_postings: 5,
  export_jobs: 6,
  tracked_applications: 7,
};

export function detail(overrides: Partial<AdminUserDetail> = {}): AdminUserDetail {
  return { ...summary(), counts: COUNTS, ...overrides };
}

export const page = (items: AdminUserSummary[], nextCursor: string | null = null): RouteHandler =>
  ok({ items, next_cursor: nextCursor });

export const userGone = (): Response =>
  jsonResponse(404, { error: { code: 'user_not_found', message: 'fixture' } });

export const REPORT = {
  base_cvs: 1,
  files: 2,
  files_unlinked: 2,
  unlink_failures: 0,
  tailoring_runs: 4,
  job_postings: 5,
  export_jobs: 6,
  tracked_applications: 7,
};

/** What a signed-out visitor's `/` needs, so a redirect there has something to render. */
export const GUEST_HOME: Record<string, RouteHandler> = {
  'GET /api/job-postings': ok({ items: [] }),
};
