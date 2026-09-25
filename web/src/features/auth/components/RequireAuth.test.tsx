import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router';

import { __resetForTests, authStore } from '../authStore';
import { RequireAuth } from './RequireAuth';

/**
 * T41 RED — the route guard (AC-41's three branches), against feature-spec.md and
 * technical-plan.md §7's routes table — never against `RequireAuth.tsx`'s T40 skeleton, which
 * ignores `children` and renders `null` unconditionally. Every branch below fails on "the expected
 * text/role is not there", never on an `ImportError`.
 *
 * Each state is driven directly through `authStore`'s own public surface (`setAuthenticated`,
 * `signOut`, `bootstrap`) — the same seam `authStore.test.ts` uses — rather than faking a
 * `useAuth()` return, because `RequireAuth` reads `useAuth()` itself and nothing here should know
 * its internals.
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

/** Reads the `next` param a redirect to `/login` carried, so a test can see it without scraping copy. */
function LoginRouteProbe(): React.JSX.Element {
  const [params] = useSearchParams();
  return <div>LOGIN PAGE next={params.get('next') ?? '(none)'}</div>;
}

function renderProtectedAt(path: string, queryClient?: QueryClient) {
  const client = queryClient ?? makeQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/account"
            element={
              <RequireAuth>
                <div>PROTECTED CONTENT</div>
              </RequireAuth>
            }
          />
          <Route path="/login" element={<LoginRouteProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const AUTHENTICATED_RESPONSE = {
  access_token: 'token-abc',
  token_type: 'Bearer' as const,
  expires_in: 900,
  user: { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-23T10:00:00Z' },
};

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('RequireAuth', () => {
  it('booting: renders a loading state, not the protected content and not a redirect', () => {
    // Fresh store defaults to `booting` — nobody has called `bootstrap()` or `refresh()` yet.
    renderProtectedAt('/account');

    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByText('PROTECTED CONTENT')).not.toBeInTheDocument();
    expect(screen.queryByText(/LOGIN PAGE/)).not.toBeInTheDocument();
  });

  it('anonymous: redirects to /login?next=/account, never rendering the protected content', () => {
    authStore.signOut('expired');

    renderProtectedAt('/account');

    expect(screen.getByText('LOGIN PAGE next=/account')).toBeInTheDocument();
    expect(screen.queryByText('PROTECTED CONTENT')).not.toBeInTheDocument();
  });

  it('unavailable: renders an error with Retry and does NOT redirect', async () => {
    makeFetchMock({
      'POST /api/auth/refresh': () => Promise.reject(new TypeError('Failed to fetch')),
    });
    await authStore.bootstrap();

    renderProtectedAt('/account');

    expect(await screen.findByRole('button', { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByText('PROTECTED CONTENT')).not.toBeInTheDocument();
    expect(screen.queryByText(/LOGIN PAGE/)).not.toBeInTheDocument();
  });

  it('authenticated: renders the protected content, with no redirect', () => {
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    makeFetchMock({
      'GET /api/auth/me': () => Promise.resolve(new Response(null, { status: 200 })),
    });

    renderProtectedAt('/account');

    expect(screen.getByText('PROTECTED CONTENT')).toBeInTheDocument();
    expect(screen.queryByText(/LOGIN PAGE/)).not.toBeInTheDocument();
  });
});
