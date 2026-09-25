import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests } from '../authStore';
import { LoginPage } from './LoginPage';

import type { ReactNode } from 'react';

/**
 * T41 RED — `/login` (AC-39, AC-43, I-53), written against feature-spec.md and technical-plan.md
 * §7's Loading/error/empty/success table — never against `LoginPage.tsx`'s T40 skeleton, which
 * renders only `<h1>Log in</h1>` and reads no hook. Every assertion below therefore fails on a
 * missing role or missing text (a label that does not exist, a button that is not there), never on
 * an `ImportError` — every import here already exists (T35–T40).
 *
 * Mounted under a real `MemoryRouter` with a `/`, `/account` and `/login` route table of its own
 * (not the app's `router.tsx`, which does not gain `/login` until T43): `LoginPage` reads `next`
 * from the URL and navigates with `useNavigate`, so a test of "did it navigate" needs somewhere for
 * that navigation to land that this file can see.
 */

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

type FetchHandler = () => Response | Promise<Response>;

/** A `fetch` stub keyed by `"<METHOD> <path>"`. An unhandled call rejects loudly. */
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

function countLoginPosts(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.filter((call) => {
    const [input, init] = call as [string | URL, RequestInit | undefined];
    const url = typeof input === 'string' ? input : input.toString();
    return url === '/api/auth/login' && (init?.method ?? 'GET') === 'POST';
  }).length;
}

function renderLoginAt(path: string, queryClient?: QueryClient) {
  const client = queryClient ?? makeQueryClient();

  function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }

  const utils = render(
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<div>HOME PAGE</div>} />
      <Route path="/account" element={<div>ACCOUNT PAGE</div>} />
    </Routes>,
    { wrapper: Wrapper },
  );
  return { ...utils, queryClient: client };
}

function fillForm(email = 'alex@example.com', password = 'a very long real password'): void {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: password } });
}

function submitButton(): HTMLElement {
  return screen.getByRole('button', { name: /log in|logging in/i });
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

describe('LoginPage', () => {
  it('AC-39/AC-45 idle: renders labeled email and password fields and an enabled submit button', () => {
    makeFetchMock({});

    renderLoginAt('/login');

    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(submitButton()).not.toBeDisabled();
  });

  it('AC-39 submitting: disables the button and both inputs, shows "Logging in…" and marks itself busy', async () => {
    makeFetchMock({
      'POST /api/auth/login': () => new Promise<Response>(() => undefined),
    });

    const { container } = renderLoginAt('/login');
    fillForm();
    fireEvent.click(submitButton());

    await waitFor(() => {
      expect(screen.getByText('Logging in…')).toBeInTheDocument();
    });
    expect(screen.getByLabelText(/email/i)).toBeDisabled();
    expect(screen.getByLabelText(/password/i)).toBeDisabled();
    expect(submitButton()).toBeDisabled();
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
  });

  it('I-53: double-clicking submit sends exactly one POST /api/auth/login', async () => {
    const fetchMock = makeFetchMock({
      'POST /api/auth/login': () => new Promise<Response>(() => undefined),
    });

    renderLoginAt('/login');
    fillForm();
    const button = submitButton();
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByText('Logging in…')).toBeInTheDocument();
    });
    expect(countLoginPosts(fetchMock)).toBe(1);
  });

  it('AC-39 success: navigates to safeNext(next) after a valid login', async () => {
    makeFetchMock({
      'POST /api/auth/login': () => jsonResponse(200, AUTHENTICATED_RESPONSE),
    });

    renderLoginAt('/login?next=/account');
    fillForm();
    fireEvent.click(submitButton());

    expect(await screen.findByText('ACCOUNT PAGE')).toBeInTheDocument();
  });

  it('AC-39/AC-43 success with an unsafe next: falls back to / rather than //evil.example', async () => {
    makeFetchMock({
      'POST /api/auth/login': () => jsonResponse(200, AUTHENTICATED_RESPONSE),
    });

    renderLoginAt('/login?next=%2F%2Fevil.example');
    fillForm();
    fireEvent.click(submitButton());

    expect(await screen.findByText('HOME PAGE')).toBeInTheDocument();
  });

  it('I-9/I-10 invalid_credentials: "That email and password don\'t match an account." in a role="alert"', async () => {
    makeFetchMock({
      'POST /api/auth/login': () =>
        jsonResponse(401, { error: { code: 'invalid_credentials', message: 'server prose' } }),
    });

    renderLoginAt('/login');
    fillForm();
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("That email and password don't match an account.");
  });

  it('I-14/I-15 rate_limited: "Too many attempts. Try again in 2 minutes." from the Retry-After header', async () => {
    makeFetchMock({
      'POST /api/auth/login': () =>
        jsonResponse(
          429,
          { error: { code: 'rate_limited', message: 'server prose' } },
          { 'Retry-After': '120' },
        ),
    });

    renderLoginAt('/login');
    fillForm();
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Too many attempts. Try again in 2 minutes.');
  });

  it('I-17 rate_limit_unavailable: "Logging in is unavailable right now. Anything you\'re doing as a guest is unaffected."', async () => {
    makeFetchMock({
      'POST /api/auth/login': () =>
        jsonResponse(503, { error: { code: 'rate_limit_unavailable', message: 'server prose' } }),
    });

    renderLoginAt('/login');
    fillForm();
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      "Logging in is unavailable right now. Anything you're doing as a guest is unaffected.",
    );
  });

  it('I-41 service_unavailable: the identical sentence rate_limit_unavailable uses (both mean "not right now")', async () => {
    makeFetchMock({
      'POST /api/auth/login': () =>
        jsonResponse(503, { error: { code: 'service_unavailable', message: 'server prose' } }),
    });

    renderLoginAt('/login');
    fillForm();
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      "Logging in is unavailable right now. Anything you're doing as a guest is unaffected.",
    );
  });

  it('I-12 invalid_email: a distinct role="alert" notice, different from invalid_credentials\' copy', async () => {
    makeFetchMock({
      'POST /api/auth/login': () =>
        jsonResponse(422, { error: { code: 'invalid_email', message: 'server prose' } }),
    });

    renderLoginAt('/login');
    fillForm('not-an-email', 'a very long real password');
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBeTruthy();
    expect(alert.textContent).not.toBe("That email and password don't match an account.");
  });

  it('network failure: "Couldn\'t reach TailorCraft."', async () => {
    makeFetchMock({
      'POST /api/auth/login': () => Promise.reject(new TypeError('Failed to fetch')),
    });

    renderLoginAt('/login');
    fillForm();
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't reach TailorCraft.");
  });

  it('every distinct-copy code (I-9/I-10, I-14/I-15, I-12, network) renders different text from each other', async () => {
    const cases: ReadonlyArray<{
      readonly name: string;
      readonly stub: FetchHandler;
    }> = [
      {
        name: 'invalid_credentials',
        stub: () => jsonResponse(401, { error: { code: 'invalid_credentials', message: 'x' } }),
      },
      {
        name: 'rate_limited',
        stub: () =>
          jsonResponse(
            429,
            { error: { code: 'rate_limited', message: 'x' } },
            { 'Retry-After': '60' },
          ),
      },
      {
        name: 'invalid_email',
        stub: () => jsonResponse(422, { error: { code: 'invalid_email', message: 'x' } }),
      },
      {
        name: 'network',
        stub: () => Promise.reject(new TypeError('Failed to fetch')),
      },
    ];

    const texts: string[] = [];
    for (const { stub } of cases) {
      makeFetchMock({ 'POST /api/auth/login': stub });
      const { unmount } = renderLoginAt('/login');
      fillForm();
      fireEvent.click(submitButton());
      const alert = await screen.findByRole('alert');
      texts.push(alert.textContent);
      unmount();
    }

    expect(new Set(texts).size).toBe(texts.length);
  });
});
