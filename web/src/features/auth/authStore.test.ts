import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { REFRESH_CONFLICT_RETRY_DELAYS_MS, __resetForTests, authStore } from './authStore';

import { countCallsTo, jsonResponse } from '@/test/fixtures';

import type { AuthenticatedResponse } from './types';

/**
 * T37 RED — `authStore`'s network-facing behaviour: AC-36 (single-flight, `<StrictMode>`-proof
 * boot), the store side of AC-38 (proactive refresh, the 409 back-off, `REFRESH_FAILED` vs
 * `BOOT_FAILED`, I-51's network/5xx path), the race rule technical plan §0.3 exists for, and AC-35's
 * "the snapshot never carries the token".
 *
 * Per the module's own docstring (the implementer's documented test seams): the store is reached
 * only through `api/auth.ts` → `client.ts` → the global `fetch`, so stubbing `fetch` counts every
 * refresh the store makes; `performance.now` is read fresh on every call, so faking it moves both
 * the deadline `setAuthenticated` writes and the check `accessTokenForRequest` makes; the 409
 * back-off is plain `setTimeout`, driven here with fake timers; and `__resetForTests()` must run in
 * `beforeEach` because the module's state outlives a single test.
 *
 * `api/client.ts`'s own interceptor behaviour (bearer attachment, the one-refresh-one-retry on
 * `invalid_access_token`, zero refreshes on `guest_session_expired`) is asserted in
 * `web/src/api/client.test.ts`, not here — this file drives the store's own public surface
 * (`bootstrap`, `refresh`, `accessTokenForRequest`, `setAuthenticated`, `getSnapshot`) directly.
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

type FetchHandler = (callNumber: number) => Response | Promise<Response>;

/**
 * A `fetch` stub keyed by `"<METHOD> <path>"`, counting calls per key (so a handler can answer
 * differently on the 2nd or 3rd call — unused here, but kept for parity with the retry tests' need
 * to assert *how many* calls happened). An unhandled call rejects loudly rather than hanging.
 */
function makeFetchMock(handlers: Record<string, FetchHandler>): ReturnType<typeof vi.fn> {
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

const [FIRST_RETRY_DELAY_MS = 300, SECOND_RETRY_DELAY_MS = 1000] = REFRESH_CONFLICT_RETRY_DELAYS_MS;

beforeEach(() => {
  vi.useRealTimers();
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('AC-36: bootstrap() is idempotent — one refresh request however many times it is called', () => {
  it("two bootstrap() calls (as <StrictMode>'s doubled effect would produce) return the same promise and send exactly one request", async () => {
    const fetchMock = makeFetchMock({
      'POST /api/auth/refresh': () => jsonResponse(200, authResponse()),
    });

    const first = authStore.bootstrap();
    const second = authStore.bootstrap();

    expect(second).toBe(first);
    await first;

    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(1);
  });

  it("bootstrap() resolves to an authenticated RefreshResult carrying the token response's user on 200", async () => {
    const response = authResponse();
    makeFetchMock({ 'POST /api/auth/refresh': () => jsonResponse(200, response) });

    const result = await authStore.bootstrap();

    expect(result).toEqual({ kind: 'authenticated', user: response.user });
    expect(authStore.getSnapshot()).toEqual({ status: 'authenticated' });
  });
});

describe('AC-36: refresh() is single-flight — concurrent callers share one in-flight request', () => {
  it('two concurrent refresh() calls return the same promise and send exactly one request', async () => {
    const fetchMock = makeFetchMock({
      'POST /api/auth/refresh': () => jsonResponse(200, authResponse()),
    });

    const a = authStore.refresh();
    const b = authStore.refresh();

    expect(b).toBe(a);
    const [resultA, resultB] = await Promise.all([a, b]);

    expect(resultA).toEqual(resultB);
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(1);
  });
});

describe('the race rule (technical plan §0.3): a login landing mid-boot beats a late boot 401', () => {
  it('boot in flight, setAuthenticated meanwhile, boot then answers 401 not_signed_in -> state stays authenticated', async () => {
    let resolveBootFetch: (response: Response) => void = () => {
      throw new Error('resolveBootFetch called before it was assigned');
    };
    const bootFetchPromise = new Promise<Response>((resolve) => {
      resolveBootFetch = resolve;
    });
    const fetchMock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      if (url === '/api/auth/refresh' && method === 'POST') {
        return bootFetchPromise;
      }
      return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
    });
    vi.stubGlobal('fetch', fetchMock);

    const bootPromise = authStore.bootstrap();

    // The boot's own request is still unresolved. A login lands in the meantime.
    authStore.setAuthenticated(authResponse({ access_token: 'login-token', expires_in: 900 }));
    expect(authStore.getSnapshot()).toEqual({ status: 'authenticated' });

    // The boot's answer finally arrives — too late to mean anything.
    resolveBootFetch(errorResponse(401, 'not_signed_in'));
    await bootPromise;

    expect(authStore.getSnapshot()).toEqual({ status: 'authenticated' });
  });
});

describe('AC-38: proactive refresh, measured on performance.now(), never the wall clock', () => {
  it('accessTokenForRequest() refreshes first when less than 30s remain, and returns the new token', async () => {
    vi.spyOn(performance, 'now').mockReturnValue(0);
    authStore.setAuthenticated(authResponse({ access_token: 'stale-token', expires_in: 20 }));

    const fetchMock = makeFetchMock({
      'POST /api/auth/refresh': () =>
        jsonResponse(200, authResponse({ access_token: 'fresh-token', expires_in: 900 })),
    });

    const token = await authStore.accessTokenForRequest();

    expect(token).toBe('fresh-token');
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(1);
  });

  it('accessTokenForRequest() makes no network call when at least 30s remain, and returns the held token', async () => {
    vi.spyOn(performance, 'now').mockReturnValue(0);
    authStore.setAuthenticated(authResponse({ access_token: 'good-token', expires_in: 900 }));

    const fetchMock = makeFetchMock({});

    const token = await authStore.accessTokenForRequest();

    expect(token).toBe('good-token');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('AC-38: a 409 refresh_in_progress is retried at 300ms and 1000ms, then the store gives up', () => {
  it('at boot: three 409s (initial + two retries) end in unavailable (BOOT_FAILED)', async () => {
    vi.useFakeTimers();
    const fetchMock = makeFetchMock({
      'POST /api/auth/refresh': () => errorResponse(409, 'refresh_in_progress'),
    });

    const bootPromise = authStore.bootstrap();
    await vi.advanceTimersByTimeAsync(FIRST_RETRY_DELAY_MS);
    await vi.advanceTimersByTimeAsync(SECOND_RETRY_DELAY_MS);
    const result = await bootPromise;

    expect(result).toEqual({ kind: 'unavailable' });
    expect(authStore.getSnapshot()).toEqual({ status: 'unavailable' });
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(3);
  });

  it('from authenticated: three 409s on a non-boot refresh() end in unavailable (REFRESH_FAILED, not anonymous)', async () => {
    vi.useFakeTimers();
    vi.spyOn(performance, 'now').mockReturnValue(0);
    authStore.setAuthenticated(authResponse({ expires_in: 900 }));

    const fetchMock = makeFetchMock({
      'POST /api/auth/refresh': () => errorResponse(409, 'refresh_in_progress'),
    });

    const refreshPromise = authStore.refresh();
    await vi.advanceTimersByTimeAsync(FIRST_RETRY_DELAY_MS);
    await vi.advanceTimersByTimeAsync(SECOND_RETRY_DELAY_MS);
    const result = await refreshPromise;

    expect(result).toEqual({ kind: 'unavailable' });
    expect(authStore.getSnapshot()).toEqual({ status: 'unavailable' });
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(3);
  });
});

describe('I-51: a network error or a 5xx at boot goes straight to unavailable, with no retry (unlike 409)', () => {
  it('a fetch rejection (network error) at boot ends in unavailable after exactly one attempt', async () => {
    const fetchMock = vi.fn((): Promise<Response> => Promise.reject(new Error('network down')));
    vi.stubGlobal('fetch', fetchMock);

    const result = await authStore.bootstrap();

    expect(result).toEqual({ kind: 'unavailable' });
    expect(authStore.getSnapshot()).toEqual({ status: 'unavailable' });
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(1);
  });

  it('a 503 service_unavailable at boot ends in unavailable after exactly one attempt (not retried like a 409)', async () => {
    const fetchMock = makeFetchMock({
      'POST /api/auth/refresh': () => errorResponse(503, 'service_unavailable'),
    });

    const result = await authStore.bootstrap();

    expect(result).toEqual({ kind: 'unavailable' });
    expect(authStore.getSnapshot()).toEqual({ status: 'unavailable' });
    expect(countCallsTo(fetchMock, '/api/auth/refresh', 'POST')).toBe(1);
  });
});

describe('AC-38 / I-48 shape: the refresh outcome table (both codes, boot vs non-boot)', () => {
  it('a 401 not_signed_in at boot moves to anonymous with no reason (a boot answer, not a session that ended)', async () => {
    makeFetchMock({ 'POST /api/auth/refresh': () => errorResponse(401, 'not_signed_in') });

    const result = await authStore.bootstrap();

    expect(result).toEqual({ kind: 'anonymous', reason: null });
    expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: null });
  });

  it('a 401 refresh_token_reused at boot also moves to anonymous with no reason', async () => {
    makeFetchMock({ 'POST /api/auth/refresh': () => errorResponse(401, 'refresh_token_reused') });

    const result = await authStore.bootstrap();

    expect(result).toEqual({ kind: 'anonymous', reason: null });
    expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: null });
  });

  it('a 401 not_signed_in on a non-boot refresh() signs out with reason "expired"', async () => {
    vi.spyOn(performance, 'now').mockReturnValue(0);
    authStore.setAuthenticated(authResponse({ expires_in: 900 }));
    makeFetchMock({ 'POST /api/auth/refresh': () => errorResponse(401, 'not_signed_in') });

    const result = await authStore.refresh();

    expect(result).toEqual({ kind: 'anonymous', reason: 'expired' });
    expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'expired' });
  });

  it('a 401 refresh_token_reused on a non-boot refresh() signs out with reason "reused"', async () => {
    vi.spyOn(performance, 'now').mockReturnValue(0);
    authStore.setAuthenticated(authResponse({ expires_in: 900 }));
    makeFetchMock({ 'POST /api/auth/refresh': () => errorResponse(401, 'refresh_token_reused') });

    const result = await authStore.refresh();

    expect(result).toEqual({ kind: 'anonymous', reason: 'reused' });
    expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'reused' });
  });
});

describe('/verify round 1 finding 1: SIGNED_OUT must supersede an in-flight non-boot refresh', () => {
  it(
    'a refresh already in flight when the user logs out must not re-authenticate them when it ' +
      'later answers 200 — the store stays anonymous, holds no token, and the refresh resolves superseded',
    async () => {
      vi.spyOn(performance, 'now').mockReturnValue(0);
      authStore.setAuthenticated(authResponse({ access_token: 'old-token', expires_in: 900 }));

      // A proactive refresh (or the client's one-retry-on-401) starts while still authenticated —
      // exactly `authStore.refresh()`'s own contract: "While not booting" (module docstring table).
      // Its request is held open under our control, so logout can land while it is still in flight.
      let resolveRefreshFetch: (response: Response) => void = () => {
        throw new Error('resolveRefreshFetch called before it was assigned');
      };
      const refreshFetchPromise = new Promise<Response>((resolve) => {
        resolveRefreshFetch = resolve;
      });
      const fetchMock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = init?.method ?? 'GET';
        if (url === '/api/auth/refresh' && method === 'POST') {
          return refreshFetchPromise;
        }
        return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
      });
      vi.stubGlobal('fetch', fetchMock);

      const inFlightRefresh = authStore.refresh();

      // The user logs out before that refresh has answered.
      authStore.signOut('logged_out');
      expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'logged_out' });

      // The stale refresh finally answers 200, carrying a token for the account that just logged out.
      resolveRefreshFetch(jsonResponse(200, authResponse({ access_token: 'late-token' })));
      const result = await inFlightRefresh;

      // The finding: `AUTHENTICATED` is honoured unconditionally (authMachine.ts's AUTHENTICATED
      // case, dispatched from authStore.ts's runRefresh 'ok' branch), so today this re-authenticates
      // the user the moment the stale refresh lands — undoing the logout they just performed.
      expect(result).toEqual({ kind: 'superseded' });
      expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'logged_out' });
      expect(await authStore.accessTokenForRequest()).toBeNull();
    },
  );
});

describe('AC-35: the snapshot never carries the access token', () => {
  it('getSnapshot() after setAuthenticated has no accessToken field, and JSON.stringify does not contain the token', () => {
    authStore.setAuthenticated(authResponse({ access_token: 'super-secret-token-xyz' }));

    const snapshot = authStore.getSnapshot();

    expect(snapshot).toEqual({ status: 'authenticated' });
    expect(JSON.stringify(snapshot)).not.toContain('super-secret-token-xyz');
  });
});
