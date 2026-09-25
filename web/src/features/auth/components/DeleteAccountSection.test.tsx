import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';

import { jsonResponse } from '@/test/fixtures';

import { currentUserQueryKey } from '../hooks/authCache';
import { __resetForTests, authStore } from '../authStore';
import { DeleteAccountSection } from './DeleteAccountSection';

import type { User } from '../types';

/**
 * T26 RED — AC-40, AC-46, against feature-spec.md and technical-plan.md §7, never against
 * `DeleteAccountSection.tsx`'s T25 skeleton, which renders `null` unconditionally. Every assertion
 * fails on "unable to find the expected role/text", never on an `ImportError`.
 *
 * **The navigation assertion is the one place this test has to guess a mechanism the spec states
 * only as "navigate to / with the notice"** — no prior page in this codebase carries a one-off
 * notice, so there is no established idiom to copy. The probe below reads `useLocation().state` and
 * prints it, which is the ordinary React Router way to pass a message to the page a redirect lands
 * on; if T27 chooses a different channel (a query param, a store), this one assertion — and only
 * this one — will need to move with it. Every other assertion in this file does not depend on it.
 */

const USER: User = { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-25T10:00:00Z' };
const AUTHENTICATED_RESPONSE = {
  access_token: 'token-1',
  token_type: 'Bearer' as const,
  expires_in: 900,
  user: USER,
};

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

type Handler = () => Response | Promise<Response>;

function makeFetchMock(handlers: Record<string, Handler>): ReturnType<typeof vi.fn> {
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

function HomeProbe(): React.JSX.Element {
  const location = useLocation();
  return <div>HOME PAGE state={JSON.stringify(location.state)}</div>;
}

function renderSection(queryClient?: QueryClient) {
  const client = queryClient ?? makeQueryClient();
  return {
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/account']}>
          <Routes>
            <Route path="/account" element={<DeleteAccountSection />} />
            <Route path="/" element={<HomeProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    queryClient: client,
  };
}

async function fillAndSubmit(password = 'correct-password') {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/your password/i), password);
  await user.click(screen.getByRole('checkbox', { name: /can't be undone/i }));
  fireEvent.click(screen.getByRole('button', { name: /delete my account/i }));
}

beforeEach(() => {
  __resetForTests();
  authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('DeleteAccountSection — AC-40, AC-46', () => {
  it('states what goes and what does not go, before any input', () => {
    makeFetchMock({});

    render(
      <QueryClientProvider client={makeQueryClient()}>
        <MemoryRouter>
          <DeleteAccountSection />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(
      screen.getByText(/deletes your account and your saved cvs.*signs you out on every device/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Copies in a browser's workspace are deleted with that workspace within 24 hours",
      ),
    ).toBeInTheDocument();
  });

  it('a labelled password field with autocomplete="current-password"', () => {
    makeFetchMock({});
    render(
      <QueryClientProvider client={makeQueryClient()}>
        <MemoryRouter>
          <DeleteAccountSection />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText(/your password/i);
    expect(input).toHaveAttribute('autocomplete', 'current-password');
  });

  it('the submit button is disabled until the confirmation checkbox is checked', async () => {
    const user = userEvent.setup();
    makeFetchMock({});
    render(
      <QueryClientProvider client={makeQueryClient()}>
        <MemoryRouter>
          <DeleteAccountSection />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await user.type(screen.getByLabelText(/your password/i), 'correct-password');
    expect(screen.getByRole('button', { name: /delete my account/i })).toBeDisabled();

    await user.click(screen.getByRole('checkbox', { name: /can't be undone/i }));
    expect(screen.getByRole('button', { name: /delete my account/i })).not.toBeDisabled();
  });

  it('pending: "Deleting your account…"', async () => {
    makeFetchMock({
      'POST /api/auth/delete-account': () => new Promise<Response>(() => undefined),
    });
    render(
      <QueryClientProvider client={makeQueryClient()}>
        <MemoryRouter>
          <DeleteAccountSection />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await fillAndSubmit();

    expect(await screen.findByText('Deleting your account…')).toBeInTheDocument();
  });

  it.each([
    ['password_incorrect', 403, /password isn't right/i],
    ['rate_limited', 429, /too many attempts/i],
    ['rate_limit_unavailable', 503, /paused for a moment/i],
    ['service_unavailable', 503, /couldn't delete your account/i],
  ] as const)('AC-40 refusal %s renders its own distinct copy', async (code, status, expected) => {
    makeFetchMock({
      'POST /api/auth/delete-account': () =>
        jsonResponse(status, { error: { code, message: 'server said so' } }),
    });
    render(
      <QueryClientProvider client={makeQueryClient()}>
        <MemoryRouter>
          <DeleteAccountSection />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await fillAndSubmit();

    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  it('every refusal leaves the user signed in — the store never moves off authenticated', async () => {
    makeFetchMock({
      'POST /api/auth/delete-account': () =>
        jsonResponse(403, { error: { code: 'password_incorrect', message: 'nope' } }),
    });
    render(
      <QueryClientProvider client={makeQueryClient()}>
        <MemoryRouter>
          <DeleteAccountSection />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await fillAndSubmit();
    await screen.findByText(/password isn't right/i);

    expect(authStore.getSnapshot().status).toBe('authenticated');
  });

  it(
    "success: the store goes anonymous (reason account_deleted), every ['auth', …] query is " +
      'removed, and the page navigates to / with a notice',
    async () => {
      makeFetchMock({
        'POST /api/auth/delete-account': () => new Response(null, { status: 204 }),
      });
      const queryClient = makeQueryClient();
      queryClient.setQueryData(currentUserQueryKey, USER);
      queryClient.setQueryData(['base-cvs'], [{ id: 'guest-cv-1' }]);

      renderSection(queryClient);
      await fillAndSubmit();

      await waitFor(() => {
        expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'account_deleted' });
      });
      expect(queryClient.getQueryData(currentUserQueryKey)).toBeUndefined();
      // Guest-workspace data is untouched — it is not under the ['auth', …] prefix (AC-40).
      expect(queryClient.getQueryData(['base-cvs'])).toEqual([{ id: 'guest-cv-1' }]);

      const home = await screen.findByText(/HOME PAGE/);
      expect(home.textContent).toContain('Your account and saved CVs were deleted.');
    },
  );

  it('success broadcasts a signed-out message so other tabs of this browser also sign out (AC-42)', async () => {
    makeFetchMock({
      'POST /api/auth/delete-account': () => new Response(null, { status: 204 }),
    });
    const channel = new BroadcastChannel('tailorcraft-auth');
    const received: unknown[] = [];
    channel.onmessage = (event: MessageEvent) => received.push(event.data);

    // Connect the store to the SAME channel the app would connect at module scope — this test
    // stands in for that wiring, since it happens in `main.tsx`, not inside the component.
    authStore.connectChannel(new BroadcastChannel('tailorcraft-auth'), () => undefined);

    renderSection();
    await fillAndSubmit();

    await waitFor(() => {
      expect(received).toContainEqual({ type: 'signed-out' });
    });
    channel.close();
  });
});
