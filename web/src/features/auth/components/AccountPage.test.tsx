import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests, authStore } from '../authStore';
import { AccountPage } from './AccountPage';
import { RequireAuth } from './RequireAuth';

/**
 * T41 RED — `/account` (AC-41's `['auth','me']` states and the Log out control; AC-44's cache
 * boundary), against feature-spec.md and technical-plan.md §7 — never against `AccountPage.tsx`'s
 * T40 skeleton, which renders only `<h1>Your account</h1>` and reads no hook.
 *
 * `AccountPage` itself does not gate on `useAuth()` — that is `RequireAuth`'s job, tested
 * separately — so every test here starts from `authStore.setAuthenticated(...)`, exactly as a real
 * mount inside `RequireAuth` would have arrived.
 */

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

type FetchHandler = () => Response | Promise<Response>;

function makeFetchMock(handlers: Record<string, FetchHandler>): ReturnType<typeof vi.fn> {
  const mock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    const handler = handlers[`${method} ${url}`];
    if (handler === undefined) {
      return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
    }
    return Promise.resolve(handler());
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function renderAccountPage(queryClient?: QueryClient) {
  const client = queryClient ?? makeQueryClient();
  return {
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <AccountPage />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    queryClient: client,
  };
}

const USER = { id: 'user-1', email: 'alex@example.com', created_at: '2026-01-05T10:00:00Z' };
const AUTHENTICATED_RESPONSE = {
  access_token: 'token-abc',
  token_type: 'Bearer' as const,
  expires_in: 900,
  user: USER,
};

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('AccountPage', () => {
  it('AC-41 loading: shows "Loading your account…" while GET /api/auth/me is in flight', () => {
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    makeFetchMock({ 'GET /api/auth/me': () => new Promise<Response>(() => undefined) });

    renderAccountPage();

    expect(screen.getByText('Loading your account…')).toBeInTheDocument();
  });

  it('AC-41 error: I-39 (401 not_signed_in) shows "Couldn\'t load your account" with Retry', async () => {
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    makeFetchMock({
      'GET /api/auth/me': () =>
        jsonResponse(401, { error: { code: 'not_signed_in', message: 'gone' } }),
    });

    renderAccountPage();

    expect(await screen.findByText("Couldn't load your account")).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('AC-41 success: shows the email, "Member since …" and a Log out button', async () => {
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    makeFetchMock({ 'GET /api/auth/me': () => jsonResponse(200, USER) });

    renderAccountPage();

    expect(await screen.findByText('alex@example.com')).toBeInTheDocument();
    expect(screen.getByText(/Member since/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /log out/i })).toBeInTheDocument();
  });

  it('AC-41 Log out pending: "Logging out…", disabled, while the request is in flight', async () => {
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'POST /api/auth/logout': () => new Promise<Response>(() => undefined),
    });

    renderAccountPage();
    fireEvent.click(await screen.findByRole('button', { name: /log out/i }));

    await waitFor(() => {
      expect(screen.getByText('Logging out…')).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /logging out/i })).toBeDisabled();
  });

  it('I-31 Log out 503: "Couldn\'t log you out. Try again." and the user STAYS logged in', async () => {
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'POST /api/auth/logout': () =>
        jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } }),
    });

    renderAccountPage();
    fireEvent.click(await screen.findByRole('button', { name: /log out/i }));

    expect(await screen.findByText("Couldn't log you out. Try again.")).toBeInTheDocument();
    // The email is still on screen — the page never treated the 503 as a successful logout.
    expect(screen.getByText('alex@example.com')).toBeInTheDocument();
    // And the store itself never moved: this is not just a stale render.
    expect(authStore.getSnapshot().status).toBe('authenticated');
  });

  it("AC-44: a successful logout removes ['auth', …] from the cache and leaves a guest-workspace query untouched", async () => {
    // gcTime: Infinity here (not makeQueryClient()'s gcTime: 0): setQueryData with no observer is
    // otherwise garbage-collected on the next macrotask regardless of what logout does, which would
    // make this assertion test cache GC rather than AC-44's scoping of removeQueries to ['auth', …].
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
    });
    queryClient.setQueryData(['base-cvs'], [{ id: 'guest-cv-1' }]);
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'POST /api/auth/logout': () => Promise.resolve(new Response(null, { status: 204 })),
    });

    renderAccountPage(queryClient);
    fireEvent.click(await screen.findByRole('button', { name: /log out/i }));

    await waitFor(() => {
      expect(queryClient.getQueryData(['auth', 'me'])).toBeUndefined();
    });
    expect(queryClient.getQueryData(['base-cvs'])).toEqual([{ id: 'guest-cv-1' }]);
  });
});

/**
 * /verify round 1 finding 2 — a deleted (or otherwise gone) user: `GET /api/auth/me` answers 401
 * `not_signed_in` while the store still says `authenticated`. Today `useCurrentUser` only surfaces
 * this as `query.isError`; nothing ends the local session, so `/account` — always reached through
 * `RequireAuth` in the real app — is stuck rendering "Couldn't load your account" with a Retry
 * button that, clicked, asks `GET /me` again and gets 401 again, forever.
 */
function renderAccountPageBehindGuard(queryClient?: QueryClient) {
  const client = queryClient ?? makeQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/account']}>
        <Routes>
          <Route
            path="/account"
            element={
              <RequireAuth>
                <AccountPage />
              </RequireAuth>
            }
          />
          <Route path="/login" element={<div>LOGIN PAGE</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('/verify round 1 finding 2: a 401 not_signed_in from GET /me must end the local session', () => {
  it(
    'signs the store out (reason "expired") instead of leaving the query merely errored — ' +
      'so a later render sees `anonymous`, not a permanently authenticated-but-broken state',
    async () => {
      authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
      makeFetchMock({
        'GET /api/auth/me': () =>
          jsonResponse(401, { error: { code: 'not_signed_in', message: 'gone' } }),
      });

      renderAccountPage();

      await waitFor(() => {
        expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'expired' });
      });
    },
  );

  it(
    'behind RequireAuth (as /account is always reached in the real app), a 401 not_signed_in ' +
      'redirects to /login instead of getting stuck on "Couldn\'t load your account" with Retry',
    async () => {
      authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
      makeFetchMock({
        'GET /api/auth/me': () =>
          jsonResponse(401, { error: { code: 'not_signed_in', message: 'gone' } }),
      });

      renderAccountPageBehindGuard();

      expect(await screen.findByText('LOGIN PAGE')).toBeInTheDocument();
      expect(screen.queryByText("Couldn't load your account")).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
    },
  );
});
