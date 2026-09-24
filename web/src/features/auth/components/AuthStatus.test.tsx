import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests, authStore } from '../authStore';
import { AuthStatus } from './AuthStatus';

/**
 * T41 RED — the header's auth block (AC-42's four states), against feature-spec.md and
 * technical-plan.md §7's table — never against `AuthStatus.tsx`'s T40 skeleton, which renders
 * `null` in every state. Every "absence" assertion below (no "Log in" link) is paired with a
 * positive one in the same test, per the task-list's own trap warning — a skeleton returning `null`
 * satisfies every absence on its own.
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

function renderStatus(queryClient?: QueryClient) {
  const client = queryClient ?? makeQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AuthStatus />
      </MemoryRouter>
    </QueryClientProvider>,
  );
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

describe('AuthStatus', () => {
  it('booting: no "Log in" link, but a busy, visually-announced "Checking your login…"', () => {
    // Fresh store: `booting`, nobody has called `bootstrap()`.
    const { container } = renderStatus();

    expect(screen.queryByRole('link', { name: /log in/i })).not.toBeInTheDocument();
    expect(screen.getByText(/checking your login/i)).toBeInTheDocument();
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
  });

  it('anonymous: "Log in" and "Create account" links, to /login and /register', () => {
    authStore.signOut('logged_out');

    renderStatus();

    expect(screen.getByRole('link', { name: 'Log in' })).toHaveAttribute('href', '/login');
    expect(screen.getByRole('link', { name: 'Create account' })).toHaveAttribute(
      'href',
      '/register',
    );
  });

  it('authenticated: the email and an "Account" link to /account', async () => {
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    makeFetchMock({ 'GET /api/auth/me': () => jsonResponse(200, USER) });

    renderStatus();

    expect(await screen.findByText('alex@example.com')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Account' })).toHaveAttribute('href', '/account');
  });

  it('unavailable: "Couldn\'t check whether you\'re logged in" + Retry, and NEVER a "Log in" link', async () => {
    makeFetchMock({
      'POST /api/auth/refresh': () => Promise.reject(new TypeError('Failed to fetch')),
    });
    await authStore.bootstrap();

    renderStatus();

    expect(await screen.findByText("Couldn't check whether you're logged in")).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /log in/i })).not.toBeInTheDocument();
  });
});
