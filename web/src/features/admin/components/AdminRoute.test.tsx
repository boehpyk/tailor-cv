import { QueryClient } from '@tanstack/react-query';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import {
  USER_A,
  callsTo,
  hang,
  ok,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

import {
  ADMIN_CHECKING,
  ADMIN_EMPTY,
  ADMIN_HEADING,
  ADMIN_RETRY_LABEL,
  ADMIN_UNAVAILABLE,
} from '../adminCopy';

import type { User } from '@/features/auth/types';

/**
 * T20 RED — slice 4.1, AC-31 (the five states of `/admin`) and AC-32 (the redirect and the landing),
 * mounted through the real route table. Written from the spec; against T19's skeleton (`AdminPage`
 * renders a fixed paragraph) each fails on "unable to find the expected role/text".
 *
 * A non-admin's 404 is Starlette's `{"detail":"Not Found"}`, not the app's envelope (api/admin.ts).
 */

const ADMIN: User = { ...USER_A, id: 'admin-1', email: 'root@example.com', role: 'admin' };

const noContent = (): Response => new Response(null, { status: 204 });
const notFound = (): Response =>
  new Response(JSON.stringify({ detail: 'Not Found' }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' },
  });

function clientWithRetriesOff(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

const NOT_FOUND_HEADING = /couldn.t find that page/i;

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('/admin — AC-31', () => {
  it('checking: a status "Checking access…" and none of the shell', async () => {
    stubAccountFetch({ ...signedInRoutes(ADMIN), 'GET /api/admin/access': hang });
    signInAs(ADMIN);

    renderWithRouter('/admin', { queryClient: clientWithRetriesOff() });

    const checking = await screen.findByText(ADMIN_CHECKING);
    expect(checking).toHaveAttribute('role', 'status');
    expect(screen.queryByRole('heading', { name: ADMIN_HEADING })).not.toBeInTheDocument();
    expect(screen.queryByText(ADMIN_EMPTY)).not.toBeInTheDocument();
  });

  it('204: the shell, an h1 "Admin" and the empty state', async () => {
    stubAccountFetch({ ...signedInRoutes(ADMIN), 'GET /api/admin/access': noContent });
    signInAs(ADMIN);

    renderWithRouter('/admin', { queryClient: clientWithRetriesOff() });

    expect(
      await screen.findByRole('heading', { level: 1, name: ADMIN_HEADING }),
    ).toBeInTheDocument();
    expect(screen.getByText(ADMIN_EMPTY)).toBeInTheDocument();
  });

  it('404: the ordinary not-found view, with no "forbidden" wording', async () => {
    stubAccountFetch({ ...signedInRoutes(USER_A), 'GET /api/admin/access': notFound });
    signInAs(USER_A);

    renderWithRouter('/admin', { queryClient: clientWithRetriesOff() });

    expect(await screen.findByRole('heading', { name: NOT_FOUND_HEADING })).toBeInTheDocument();
    expect(
      screen.queryByText(/forbidden|not allowed|permission|admin only/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: ADMIN_HEADING })).not.toBeInTheDocument();
  });

  it('a 404 is not retried', async () => {
    const fetch = stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/admin/access': notFound,
    });
    signInAs(USER_A);

    renderWithRouter('/admin', { queryClient: clientWithRetriesOff() });
    await screen.findByRole('heading', { name: NOT_FOUND_HEADING });

    // TanStack's retry backoff for the first retry is one second.
    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(callsTo(fetch, 'GET', '/api/admin/access')).toHaveLength(1);
  });

  it('503: retried once, then "Couldn\'t check admin access." as an alert with Retry (not "checking")', async () => {
    const fetch = stubAccountFetch({
      ...signedInRoutes(ADMIN),
      'GET /api/admin/access': status(503, 'service_unavailable'),
    });
    signInAs(ADMIN);

    renderWithRouter('/admin', { queryClient: clientWithRetriesOff() });

    const alert = await screen.findByRole('alert', {}, { timeout: 4000 });
    expect(alert).toHaveTextContent(ADMIN_UNAVAILABLE);
    expect(screen.getByRole('button', { name: ADMIN_RETRY_LABEL })).toBeInTheDocument();
    expect(screen.queryByText(ADMIN_CHECKING)).not.toBeInTheDocument();
    expect(callsTo(fetch, 'GET', '/api/admin/access')).toHaveLength(2);
  });

  it('Retry refetches, and a 204 then shows the shell', async () => {
    const fetch = stubAccountFetch({
      ...signedInRoutes(ADMIN),
      'GET /api/admin/access': (_call, n) =>
        n <= 2 ? status(503, 'service_unavailable')() : noContent(),
    });
    signInAs(ADMIN);

    renderWithRouter('/admin', { queryClient: clientWithRetriesOff() });
    const retry = await screen.findByRole('button', { name: ADMIN_RETRY_LABEL }, { timeout: 4000 });
    const before = callsTo(fetch, 'GET', '/api/admin/access').length;
    await userEvent.setup().click(retry);

    expect(
      await screen.findByRole('heading', { level: 1, name: ADMIN_HEADING }),
    ).toBeInTheDocument();
    expect(callsTo(fetch, 'GET', '/api/admin/access').length).toBeGreaterThan(before);
  });

  it('404 after an earlier 204 (a demotion caught on focus): not-found wins over the cached 204', async () => {
    const fetch = stubAccountFetch({
      ...signedInRoutes(ADMIN),
      'GET /api/admin/access': (_call, n) => (n === 1 ? noContent() : notFound()),
    });
    signInAs(ADMIN);

    renderWithRouter('/admin', { queryClient: clientWithRetriesOff() });
    expect(
      await screen.findByRole('heading', { level: 1, name: ADMIN_HEADING }),
    ).toBeInTheDocument();

    fireEvent(document, new Event('visibilitychange'));

    expect(await screen.findByRole('heading', { name: NOT_FOUND_HEADING })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: ADMIN_HEADING })).not.toBeInTheDocument();
    expect(callsTo(fetch, 'GET', '/api/admin/access')).toHaveLength(2);
  });
});

describe('/admin — AC-32', () => {
  it('anonymous is redirected to /login?next=%2Fadmin', async () => {
    stubAccountFetch({});
    authStore.signOut('logged_out');

    const { router } = renderWithRouter('/admin', { queryClient: clientWithRetriesOff() });

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/login');
    });
    expect(router.state.location.search).toBe('?next=%2Fadmin');
  });

  async function logInAt(user: User): Promise<ReturnType<typeof renderWithRouter>> {
    stubAccountFetch({
      ...signedInRoutes(user),
      'POST /api/auth/login': ok({
        access_token: `token-${user.id}`,
        token_type: 'Bearer',
        expires_in: 900,
        user,
      }),
      'GET /api/admin/access': user.role === 'admin' ? noContent : notFound,
    });
    authStore.signOut('logged_out');
    const view = renderWithRouter('/login?next=%2Fadmin', { queryClient: clientWithRetriesOff() });
    fireEvent.change(await screen.findByLabelText(/email/i), { target: { value: user.email } });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'a very long real password' },
    });
    fireEvent.click(screen.getByRole('button', { name: /log in/i }));
    return view;
  }

  it('an admin who logs in lands on /admin and sees the shell', async () => {
    const { router } = await logInAt(ADMIN);

    expect(
      await screen.findByRole('heading', { level: 1, name: ADMIN_HEADING }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/admin');
  });

  it('a plain user who logs in lands on the not-found view', async () => {
    const { router } = await logInAt(USER_A);

    expect(await screen.findByRole('heading', { name: NOT_FOUND_HEADING })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/admin');
    expect(screen.queryByRole('heading', { name: ADMIN_HEADING })).not.toBeInTheDocument();
  });
});
