import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests, authStore } from '@/features/auth/authStore';

import { PICKER_UNAVAILABLE_NOTE, SAVED_CVS_LOAD_ERROR_NOTE } from '../savedCvsCopy';
import { SavedBaseCvPicker } from './SavedBaseCvPicker';

import type { User } from '@/features/auth/types';

/**
 * T26 RED — AC-38, AC-39, AC-45, against feature-spec.md and technical-plan.md §7, never against
 * `SavedBaseCvPicker.tsx`'s T25 skeleton, which renders `null` unconditionally. Every assertion
 * fails on "unable to find the expected role/text" (or, for the anonymous/booting cases, on the
 * discriminating *positive* control below proving the harness itself can render something), never
 * on an `ImportError`.
 *
 * Slice 2.4 (T33, AC-44) removed the picker's copy mode and, with it, every test of *Use this CV*,
 * its retention notice and its empty-state link. What remains is the auth gate, rendered in the
 * picker's one mode, `select`.
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

type Handler = (init: RequestInit | undefined, callNumber: number) => Response | Promise<Response>;

function makeFetchMock(handlers: Record<string, Handler>) {
  const counts = new Map<string, number>();
  const mock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    const key = `${method} ${url}`;
    const handler = handlers[key];
    if (handler === undefined) {
      return Promise.reject(new Error(`unhandled fetch: ${key}`));
    }
    const callNumber = (counts.get(key) ?? 0) + 1;
    counts.set(key, callNumber);
    return Promise.resolve(handler(init, callNumber));
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function renderPicker(queryClient?: QueryClient) {
  const client = queryClient ?? makeQueryClient();
  return {
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <SavedBaseCvPicker mode="select" chosenId={null} onChoose={() => undefined} />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    queryClient: client,
  };
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('SavedBaseCvPicker — AC-38 auth gate', () => {
  it('anonymous: renders nothing at all', () => {
    makeFetchMock({});
    authStore.signOut('expired');

    const { container } = renderPicker();

    expect(container).toBeEmptyDOMElement();
  });

  it('booting: renders nothing (no flicker) — a fresh store defaults to booting', () => {
    makeFetchMock({});

    const { container } = renderPicker();

    expect(container).toBeEmptyDOMElement();
  });

  it('unavailable: "Couldn\'t check your account…" + Retry', async () => {
    makeFetchMock({
      'POST /api/auth/refresh': () => Promise.reject(new TypeError('Failed to fetch')),
    });
    await authStore.bootstrap();

    renderPicker();

    expect(await screen.findByText(PICKER_UNAVAILABLE_NOTE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('authenticated + error: the same load-failure copy as the account section, plus Retry', async () => {
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () =>
        jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } }),
    });
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);

    renderPicker();

    expect(await screen.findByText(SAVED_CVS_LOAD_ERROR_NOTE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });
});
