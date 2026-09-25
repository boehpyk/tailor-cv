import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { request } from './client';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { countCallsTo, jsonResponse } from '@/test/fixtures';

import type { AuthenticatedResponse } from '@/features/auth/types';

/**
 * T37 RED (AC-38) — `api/client.ts`'s bearer interceptor, driven end-to-end through the real
 * `authStore` (only `__resetForTests`/`setAuthenticated` are test seams; the interceptor itself is
 * exercised exactly as a component would reach it, through `request()`).
 *
 * Per the implementer's documented seam: the store reaches the network only through this module's
 * `fetch`, so a `vi.stubGlobal('fetch', …)` here counts every request *and* every refresh the
 * interceptor triggers underneath it — there is one network boundary, and stubbing it is enough.
 *
 * The store's own refresh mechanics (single-flight, the 409 back-off, `BOOT_FAILED` vs
 * `REFRESH_FAILED`) are asserted in `features/auth/authStore.test.ts`; this file asserts only what
 * `client.ts` itself decides: whether a request carries `Authorization` at all, and when a 401
 * triggers the one-refresh-one-retry.
 */

function authResponse(overrides: Partial<AuthenticatedResponse> = {}): AuthenticatedResponse {
  return {
    access_token: 'access-token-1',
    token_type: 'Bearer',
    expires_in: 900,
    user: { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-23T10:00:00Z' },
    ...overrides,
  };
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: code } });
}

function headerValue(init: RequestInit | undefined, name: string): string | null {
  return new Headers(init?.headers).get(name);
}

type FetchHandler = (callNumber: number) => Response | Promise<Response>;

/**
 * A `fetch` stub keyed by `"<METHOD> <path>"`, counting calls per key so a handler can answer
 * differently across attempts (the 401-then-200 shape the retry tests need). An unhandled call
 * rejects loudly rather than hanging.
 */
function makeFetchMock(handlers: Record<string, FetchHandler>) {
  const counts = new Map<string, number>();
  const mock = vi.fn(async (input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    const key = `${method} ${url}`;
    const handler = handlers[key];
    if (handler === undefined) {
      throw new Error(`unhandled fetch: ${key}`);
    }
    const callNumber = (counts.get(key) ?? 0) + 1;
    counts.set(key, callNumber);
    return handler(callNumber);
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

beforeEach(() => {
  vi.useRealTimers();
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('AC-38: only auth:"required" requests carry Authorization', () => {
  it('a guest request carries no Authorization header; an auth:"required" request carries the bearer token', async () => {
    vi.spyOn(performance, 'now').mockReturnValue(0);
    authStore.setAuthenticated(authResponse({ access_token: 'bearer-token-1', expires_in: 900 }));

    const fetchMock = makeFetchMock({
      'GET /api/guest-thing': () => jsonResponse(200, { ok: true }),
      'GET /api/protected-thing': () => jsonResponse(200, { ok: true }),
    });

    await request('/api/guest-thing');
    await request('/api/protected-thing', { auth: 'required' });

    const guestCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/guest-thing');
    const authCall = fetchMock.mock.calls.find(
      ([input]) => String(input) === '/api/protected-thing',
    );

    expect(headerValue(guestCall?.[1], 'Authorization')).toBeNull();
    expect(headerValue(authCall?.[1], 'Authorization')).toBe('Bearer bearer-token-1');
  });
});

describe('AC-38: a 401 invalid_access_token gets exactly one refresh and one retry, then gives up', () => {
  it('refreshes once, retries once, and surfaces a second 401 as an error with no second refresh', async () => {
    vi.spyOn(performance, 'now').mockReturnValue(0);
    authStore.setAuthenticated(authResponse({ access_token: 'stale-token', expires_in: 900 }));

    const fetchMock = makeFetchMock({
      'GET /api/protected-thing': () => errorResponse(401, 'invalid_access_token'),
      'POST /api/auth/refresh': () =>
        jsonResponse(200, authResponse({ access_token: 'fresh-token', expires_in: 900 })),
    });

    await expect(request('/api/protected-thing', { auth: 'required' })).rejects.toMatchObject({
      code: 'invalid_access_token',
    });

    expect(countCallsTo(fetchMock, '/api/protected-thing', 'GET')).toBe(2);
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(1);
  });

  it('a request that succeeds after the retry resolves normally, using the refreshed token', async () => {
    vi.spyOn(performance, 'now').mockReturnValue(0);
    authStore.setAuthenticated(authResponse({ access_token: 'stale-token', expires_in: 900 }));

    const fetchMock = makeFetchMock({
      'GET /api/protected-thing': (callNumber) =>
        callNumber === 1
          ? errorResponse(401, 'invalid_access_token')
          : jsonResponse(200, { ok: true }),
      'POST /api/auth/refresh': () =>
        jsonResponse(200, authResponse({ access_token: 'fresh-token', expires_in: 900 })),
    });

    const result = await request<{ readonly ok: boolean }>('/api/protected-thing', {
      auth: 'required',
    });

    expect(result).toEqual({ ok: true });
    expect(countCallsTo(fetchMock, '/api/protected-thing', 'GET')).toBe(2);
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(1);

    const secondAttempt = fetchMock.mock.calls
      .filter(([input]) => String(input) === '/api/protected-thing')
      .at(1);
    expect(headerValue(secondAttempt?.[1], 'Authorization')).toBe('Bearer fresh-token');
  });
});

describe('I-48: a 401 guest_session_expired never triggers a refresh — the branch is on code, not status', () => {
  it('from a guest request (no auth declared)', async () => {
    const fetchMock = makeFetchMock({
      'GET /api/guest-thing': () => errorResponse(401, 'guest_session_expired'),
    });

    await expect(request('/api/guest-thing')).rejects.toMatchObject({
      code: 'guest_session_expired',
    });

    expect(countCallsTo(fetchMock, '/api/guest-thing', 'GET')).toBe(1);
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(0);
  });

  it('from an auth:"required" request too — the same code means the same thing everywhere', async () => {
    vi.spyOn(performance, 'now').mockReturnValue(0);
    authStore.setAuthenticated(authResponse({ access_token: 'token', expires_in: 900 }));

    const fetchMock = makeFetchMock({
      'GET /api/protected-thing': () => errorResponse(401, 'guest_session_expired'),
    });

    await expect(request('/api/protected-thing', { auth: 'required' })).rejects.toMatchObject({
      code: 'guest_session_expired',
    });

    expect(countCallsTo(fetchMock, '/api/protected-thing', 'GET')).toBe(1);
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(0);
  });
});
