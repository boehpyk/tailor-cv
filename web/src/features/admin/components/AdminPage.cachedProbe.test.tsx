import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import {
  USER_A,
  callsTo,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { adminOnly, firewallNotFound, noContent, page } from '@/test/adminFetch';
import { renderWithRouter } from '@/test/render';

import { ADMIN_UNAVAILABLE, USERS_HEADING } from '../adminCopy';

import type { RouteHandler } from '@/test/accountFetch';
import type { User } from '@/features/auth/types';

/**
 * T20 RED — slice 4.2, AC-37 (OQ-5): which errors outrank a cached 204. Only a 404 does. Written
 * against the app's own client (`createAppQueryClient()`, 4.1's F-1) and a focus event on `window`.
 * Against T19, `AdminPage` is still error-first, so a 503 on the refetch replaces the page.
 */

const ADMIN: User = { ...USER_A, id: 'admin-1', email: 'root@example.com', role: 'admin' };
const NOT_FOUND_HEADING = /couldn.t find that page/i;

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function adminThenRefetchAnswers(
  refetch: RouteHandler,
): Promise<ReturnType<typeof stubAccountFetch>> {
  const fetch = stubAccountFetch({
    ...signedInRoutes(ADMIN),
    'GET /api/admin/access': (call, n) => (n === 1 ? noContent() : refetch(call, n)),
    'GET /api/admin/users': adminOnly(ADMIN, page([])),
  });
  signInAs(ADMIN);
  renderWithRouter('/admin', { queryClient: createAppQueryClient() });
  await screen.findByRole('heading', { level: 1, name: USERS_HEADING });
  fireEvent(window, new Event('visibilitychange'));
  return fetch;
}

describe('AC-37 — the probe refetch with a cached 204', () => {
  it('a 503 keeps the admin page on screen (after its one retry)', async () => {
    const fetch = await adminThenRefetchAnswers(status(503, 'service_unavailable'));

    // First refetch + the hook's one retry (one second of backoff) = three probe calls.
    await waitFor(
      () => {
        expect(callsTo(fetch, 'GET', '/api/admin/access')).toHaveLength(3);
      },
      { timeout: 4000 },
    );
    await new Promise((resolve) => setTimeout(resolve, 100));

    expect(screen.getByRole('heading', { level: 1, name: USERS_HEADING })).toBeInTheDocument();
    expect(screen.queryByText(ADMIN_UNAVAILABLE)).not.toBeInTheDocument();
  });

  it('a 404 still replaces it with the not-found page', async () => {
    await adminThenRefetchAnswers(firewallNotFound);

    expect(await screen.findByRole('heading', { name: NOT_FOUND_HEADING })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: USERS_HEADING })).not.toBeInTheDocument();
  });
});
