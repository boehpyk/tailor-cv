import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';

import { CHECK_EMAIL_HEADING, SENDING_LABEL } from '@/features/accountMail/accountMailCopy';
import { jsonResponse } from '@/test/fixtures';

import { __resetForTests, authStore } from '../authStore';
import { RegisterPage } from './RegisterPage';

import type { ReactNode } from 'react';

/**
 * **Amended at slice 2.5's T37 RED** (AC-45, plan §7: registering no longer signs in). Four tests
 * changed meaning and one was removed, each named in that commit's body: the pending label is
 * "Sending…" (not "Creating your account…"); a success is a 202 that shows "Check your email" and
 * navigates nowhere (it was a 201 that landed on `safeNext`); `email_already_registered` can no
 * longer come from `/register`, so I-5 is deleted and the distinctness list loses its fourth case.
 *
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
  return screen.getByRole('button', { name: /create account|sending/i });
}

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

  it('AC-40 submitting: disables the button and both inputs and shows "Sending…" (2.5: the account is not created yet)', async () => {
    makeFetchMock({
      'POST /api/auth/register': () => new Promise<Response>(() => undefined),
    });

    const { container } = renderRegisterAt('/register');
    fillForm();
    fireEvent.click(submitButton());

    await waitFor(() => {
      expect(screen.getByText(SENDING_LABEL)).toBeInTheDocument();
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
      expect(screen.getByText(SENDING_LABEL)).toBeInTheDocument();
    });
    expect(countRegisterPosts(fetchMock)).toBe(1);
  });

  it('AC-45 success: a 202 shows "Check your email" in place and signs nobody in (2.5; was: a 201 navigated to safeNext)', async () => {
    makeFetchMock({
      'POST /api/auth/register': () => new Response(null, { status: 202 }),
    });

    renderRegisterAt('/register?next=/account');
    fillForm();
    fireEvent.click(submitButton());

    expect(await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING })).toBeInTheDocument();
    expect(screen.queryByText('ACCOUNT PAGE')).not.toBeInTheDocument();
    expect(authStore.getSnapshot()).toEqual({ status: 'booting' });
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

  it('password_too_short, password_too_long and password_matches_email render distinct text', async () => {
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
