import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { routes } from '@/router';
import { jsonResponse } from '@/test/fixtures';

import { ACCOUNT_DELETED_NOTICE } from '@/features/savedCvs/savedCvsCopy';

import { __resetForTests, authStore } from '../authStore';

import type { DataRouter } from 'react-router';
import type { User } from '../types';

/**
 * T29 (`qa`, test-after) — pins a behaviour T28 built and only checked by hand (see
 * `useDeleteAccount.ts`'s own docstring: "Measured against the real route table (T28)"): a visitor
 * whose account was just deleted is anonymous with reason `account_deleted`, and `RequireAuth`
 * sends that reason to `/` carrying AC-40's notice — never to `/login?next=/account`, which would
 * offer to log back in to an account that no longer exists.
 *
 * **Against the real route table.** `router.tsx`'s own `routes` mounted on a `createMemoryRouter`
 * at `/account`, under `<StrictMode>` (matching `main.tsx`), driving a real `DeleteAccountSection`
 * submit with only `fetch` mocked — not a stand-in login/account probe page, so the redirect target
 * is proven to be the app's actual `/` route (`WorkspacePage`, which renders `AccountDeletedNotice`)
 * and the app's actual `/login` route (`LoginPage`), not a fixture that merely resembles them.
 *
 * **Discriminates — proven by mutation, both outcomes recorded.** The `account_deleted` branch in
 * `RequireAuth.tsx` was temporarily replaced with a fall-through to the ordinary
 * `/login?next=...` redirect (i.e. `auth.reason === 'account_deleted'` treated like any other
 * reason):
 *
 * - **Mutated (red):** `should land on / with the notice, shown once, and cleared from history so
 *   a reload does not show it again` failed —
 *   `TestingLibraryElementError: Unable to find an element with the text: Your account and saved
 *   CVs were deleted.` The router had instead landed on `/login?next=%2Faccount`, and
 *   `screen.getByRole('heading', { name: /log in/i })` was findable where the notice was expected.
 * - **Restored (green):** the same test passes; `git diff --exit-code web/src` is empty apart from
 *   this test file (confirmed after restoring `RequireAuth.tsx` byte-exact from `git show
 *   HEAD:web/src/features/auth/components/RequireAuth.tsx`).
 *
 * The companion test below (a plain `expired` sign-out still lands on `/login`) is unchanged by
 * that mutation, which is the point: the mutation only touches the `account_deleted` branch, and a
 * test that moved together with it would not be discriminating between the two reasons.
 */

const USER: User = { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-23T10:00:00Z' };
const AUTHENTICATED_RESPONSE = {
  access_token: 'token-1',
  token_type: 'Bearer' as const,
  expires_in: 900,
  user: USER,
};

/** Every request the workspace's own lists need once the store goes back to anonymous. */
const EMPTY_GUEST_WORKSPACE_HANDLERS = {
  'GET /api/base-cvs': () => jsonResponse(200, { items: [] }),
  'GET /api/job-postings': () => jsonResponse(200, { items: [] }),
  'GET /api/tailoring-runs': () => jsonResponse(200, { items: [] }),
} as const;

type Handler = () => Response | Promise<Response>;

function stubFetch(handlers: Record<string, Handler>): void {
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
}

/** Mounts the app's real route table under `<StrictMode>`, matching `main.tsx`. */
function renderRealAppAt(path: string, queryClient: QueryClient): { router: DataRouter } {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </StrictMode>,
  );
  return { router };
}

/** A fresh mount of the same `router` — the closest a memory router gets to "reload this page": the
 * URL and its (already-cleared) history state persist, but the component tree starts over. */
function remountAt(router: DataRouter, queryClient: QueryClient): void {
  render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </StrictMode>,
  );
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('RequireAuth — the account-deleted landing, against the real route table (T29)', () => {
  it(
    'a successful account deletion at /account lands on / with the notice, shown once, and ' +
      'clears it from history so a reload does not show it again',
    async () => {
      stubFetch({
        'GET /api/auth/me': () => jsonResponse(200, USER),
        'GET /api/me/base-cvs': () => jsonResponse(200, { items: [] }),
        'POST /api/auth/delete-account': () => new Response(null, { status: 204 }),
        ...EMPTY_GUEST_WORKSPACE_HANDLERS,
      });
      authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
      const queryClient = makeQueryClient();

      const { router } = renderRealAppAt('/account', queryClient);

      const passwordInput = await screen.findByLabelText(/your password/i);
      await userEvent.type(passwordInput, 'correct-password');
      await userEvent.click(screen.getByRole('checkbox', { name: /can't be undone/i }));
      fireEvent.click(screen.getByRole('button', { name: /delete my account/i }));

      await waitFor(() => {
        expect(router.state.location.pathname).toBe('/');
      });
      expect(await screen.findByText(ACCOUNT_DELETED_NOTICE)).toBeInTheDocument();
      // Never the login form — there is no account left to log back in to.
      expect(screen.queryByRole('heading', { name: /log in/i })).not.toBeInTheDocument();

      // AccountDeletedNotice's own effect replaces the history entry with `state: null` once shown.
      await waitFor(() => {
        expect(router.state.location.state).toBeNull();
      });

      // "A reload/second visit doesn't show it": a fresh mount at the same (now state-less) entry.
      cleanup();
      remountAt(router, queryClient);

      expect(screen.queryByText(ACCOUNT_DELETED_NOTICE)).not.toBeInTheDocument();
    },
  );

  it('a plain sign-out for another reason still lands on /login, never on /', () => {
    stubFetch({});
    authStore.signOut('expired');
    const queryClient = makeQueryClient();

    const { router } = renderRealAppAt('/account', queryClient);

    expect(router.state.location.pathname).toBe('/login');
    expect(router.state.location.search).toBe('?next=%2Faccount');
    expect(screen.getByRole('heading', { name: /log in/i })).toBeInTheDocument();
    expect(screen.queryByText(ACCOUNT_DELETED_NOTICE)).not.toBeInTheDocument();
  });
});
