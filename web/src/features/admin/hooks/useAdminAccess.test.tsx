import { QueryClient } from '@tanstack/react-query';
import { screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authQueryKeyPrefix } from '@/features/auth/hooks/authCache';
import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, callsTo, signInAs, signedInRoutes, stubAccountFetch } from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

import { ADMIN_HEADING } from '../adminCopy';
import { adminAccessQueryKey } from './useAdminAccess';

/** T22 — AC-34: the probe's key and the server being asked whatever the cached role says. */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the admin access probe — AC-34', () => {
  it("its key is ['auth','account',userId,'admin','access'], spelled out", () => {
    expect(adminAccessQueryKey('u-1')).toEqual(['auth', 'account', 'u-1', 'admin', 'access']);
  });

  it("sign-out's removeQueries(['auth']) drops it, and only this user's copy is keyed by this user", () => {
    const client = new QueryClient();
    client.setQueryData(adminAccessQueryKey('u-1'), true);

    client.removeQueries({ queryKey: authQueryKeyPrefix });

    expect(client.getQueryData(adminAccessQueryKey('u-1'))).toBeUndefined();
  });

  it('two accounts never share an entry', () => {
    const client = new QueryClient();
    client.setQueryData(adminAccessQueryKey('u-1'), true);

    expect(client.getQueryData(adminAccessQueryKey('u-2'))).toBeUndefined();
  });

  it('/admin asks the server even though the cached /me role is "user" (OQ-13)', async () => {
    expect(USER_A.role).toBe('user');
    const fetch = stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/admin/access': () => new Response(null, { status: 204 }),
    });
    signInAs(USER_A);

    renderWithRouter('/admin');

    // A just-promoted admin with a stale /me still gets the screen: the server is the control.
    expect(
      await screen.findByRole('heading', { level: 1, name: ADMIN_HEADING }),
    ).toBeInTheDocument();
    expect(callsTo(fetch, 'GET', '/api/admin/access')).toHaveLength(1);
  });
});
