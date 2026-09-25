import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests } from '../authStore';
import { RegisterPage } from './RegisterPage';

import type { ReactNode } from 'react';

/**
 * T41 RED — `/register` (AC-40, AC-43), against feature-spec.md and technical-plan.md §7's table —
 * never against `RegisterPage.tsx`'s T40 skeleton (`<h1>Create account</h1>` and nothing else).
 * Every assertion fails on missing text or a missing role, not on an `ImportError`.
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

function countRegisterPosts(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.filter((call) => {
    const [input, init] = call as [string | URL, RequestInit | undefined];
    const url = typeof input === 'string' ? input : input.toString();
    return url === '/api/auth/register' && (init?.method ?? 'GET') === 'POST';
  }).length;
}

function renderRegisterAt(path: string, queryClient?: QueryClient) {
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
      <Route path="/register" element={<RegisterPage />} />
      <Route path="/login" element={<div>LOGIN PAGE</div>} />
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
  return screen.getByRole('button', { name: /create account|creating your account/i });
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

describe('RegisterPage', () => {
  it('AC-40 idle: renders labeled email and password fields, an enabled submit button, and both static notices', () => {
    makeFetchMock({});

    renderRegisterAt('/register');

    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(submitButton()).not.toBeDisabled();
    expect(
      screen.getByText(/your email address and a one-way hash of your password/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/work you did as a guest stays with this browser for 24 hours/i),
    ).toBeInTheDocument();
  });

  it('AC-40 submitting: disables the button and both inputs and shows "Creating your account…"', async () => {
    makeFetchMock({
      'POST /api/auth/register': () => new Promise<Response>(() => undefined),
    });

    const { container } = renderRegisterAt('/register');
    fillForm();
    fireEvent.click(submitButton());

    await waitFor(() => {
      expect(screen.getByText('Creating your account…')).toBeInTheDocument();
    });
    expect(screen.getByLabelText(/email/i)).toBeDisabled();
    expect(screen.getByLabelText(/password/i)).toBeDisabled();
    expect(submitButton()).toBeDisabled();
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
  });

  it('I-53 no double submit: double-clicking sends exactly one POST /api/auth/register', async () => {
    const fetchMock = makeFetchMock({
      'POST /api/auth/register': () => new Promise<Response>(() => undefined),
    });

    renderRegisterAt('/register');
    fillForm();
    const button = submitButton();
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByText('Creating your account…')).toBeInTheDocument();
    });
    expect(countRegisterPosts(fetchMock)).toBe(1);
  });

  it('AC-40 success: navigates to safeNext(next) after registering', async () => {
    makeFetchMock({
      'POST /api/auth/register': () => jsonResponse(201, AUTHENTICATED_RESPONSE),
    });

    renderRegisterAt('/register?next=/account');
    fillForm();
    fireEvent.click(submitButton());

    expect(await screen.findByText('ACCOUNT PAGE')).toBeInTheDocument();
  });

  it('I-2 password_too_short: "Use at least 13 characters." — the SERVER\'S min_length, not a hard-coded 12', async () => {
    makeFetchMock({
      'POST /api/auth/register': () =>
        jsonResponse(422, {
          error: { code: 'password_too_short', message: 'server prose', min_length: 13 },
        }),
    });

    renderRegisterAt('/register');
    fillForm();
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Use at least 13 characters.');
  });

  it("I-3 password_too_long: the alert carries the server's max_length number", async () => {
    makeFetchMock({
      'POST /api/auth/register': () =>
        jsonResponse(422, {
          error: { code: 'password_too_long', message: 'server prose', max_length: 128 },
        }),
    });

    renderRegisterAt('/register');
    fillForm();
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/128/);
  });

  it('I-4 password_matches_email: a distinct role="alert" notice', async () => {
    makeFetchMock({
      'POST /api/auth/register': () =>
        jsonResponse(422, { error: { code: 'password_matches_email', message: 'server prose' } }),
    });

    renderRegisterAt('/register');
    fillForm('alex@example.com', 'alex@example.com');
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBeTruthy();
  });

  it('I-5 email_already_registered: "An account with this email already exists." with a Log in link to /login', async () => {
    makeFetchMock({
      'POST /api/auth/register': () =>
        jsonResponse(409, { error: { code: 'email_already_registered', message: 'server prose' } }),
    });

    renderRegisterAt('/register');
    fillForm();
    fireEvent.click(submitButton());

    expect(
      await screen.findByText('An account with this email already exists.'),
    ).toBeInTheDocument();
    const loginLink = screen.getByRole('link', { name: /log in/i });
    expect(loginLink).toHaveAttribute('href', '/login');
  });

  it('network failure: "Couldn\'t reach TailorCraft."', async () => {
    makeFetchMock({
      'POST /api/auth/register': () => Promise.reject(new TypeError('Failed to fetch')),
    });

    renderRegisterAt('/register');
    fillForm();
    fireEvent.click(submitButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't reach TailorCraft.");
  });

  it('password_too_short, password_too_long, password_matches_email and email_already_registered render distinct text', async () => {
    const cases: ReadonlyArray<{ readonly stub: FetchHandler }> = [
      {
        stub: () =>
          jsonResponse(422, {
            error: { code: 'password_too_short', message: 'x', min_length: 13 },
          }),
      },
      {
        stub: () =>
          jsonResponse(422, {
            error: { code: 'password_too_long', message: 'x', max_length: 128 },
          }),
      },
      {
        stub: () => jsonResponse(422, { error: { code: 'password_matches_email', message: 'x' } }),
      },
      {
        stub: () =>
          jsonResponse(409, { error: { code: 'email_already_registered', message: 'x' } }),
      },
    ];

    const texts: string[] = [];
    for (const { stub } of cases) {
      makeFetchMock({ 'POST /api/auth/register': stub });
      const { unmount } = renderRegisterAt('/register');
      fillForm();
      fireEvent.click(submitButton());
      const alert = await screen.findByRole('alert');
      texts.push(alert.textContent);
      unmount();
    }

    expect(new Set(texts).size).toBe(texts.length);
  });
});
