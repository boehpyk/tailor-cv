import { vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';

import { jsonResponse, makeRun } from './fixtures';

import type { User } from '@/features/auth/types';
import type { HistoryEntry, HistoryPage } from '@/features/history/types';
import type { TailoringRun } from '@/features/tailoring/types';

/**
 * Slice 2.3 test support (T30): a `fetch` stub for the account workspace, history and re-open
 * surfaces that **records every call** — method, path, query and the `Authorization` header — so a
 * test can assert *where* a request went and *with which credential* (AC-48), not only that the UI
 * changed.
 *
 * Routes are `'METHOD /path'`, where a segment written `:name` matches any one segment; the query
 * string is not part of the key (a handler reads it from the call). An unrouted request rejects
 * loudly, except `GET /health/ready`, which hangs — the layout's status panel polls it on every
 * route and nothing here is about it.
 */

export interface RecordedCall {
  readonly method: string;
  readonly path: string;
  readonly query: URLSearchParams;
  readonly authorization: string | null;
  readonly body: unknown;
}

export type RouteHandler = (call: RecordedCall, callNumber: number) => Response | Promise<Response>;

export interface AccountFetch {
  readonly calls: RecordedCall[];
  readonly mock: ReturnType<typeof vi.fn>;
}

function headerOf(init: RequestInit | undefined, name: string): string | null {
  const headers = init?.headers;
  if (headers === undefined) {
    return null;
  }
  if (headers instanceof Headers) {
    return headers.get(name);
  }
  if (Array.isArray(headers)) {
    const found = headers.find(([key]) => key.toLowerCase() === name.toLowerCase());
    return found === undefined ? null : found[1];
  }
  const record = headers;
  const key = Object.keys(record).find(
    (candidate) => candidate.toLowerCase() === name.toLowerCase(),
  );
  return key === undefined ? null : (record[key] ?? null);
}

function bodyOf(init: RequestInit | undefined): unknown {
  if (typeof init?.body !== 'string') {
    return init?.body ?? null;
  }
  try {
    return JSON.parse(init.body) as unknown;
  } catch {
    return init.body;
  }
}

function matches(pattern: string, path: string): boolean {
  const want = pattern.split('/');
  const got = path.split('/');
  return (
    want.length === got.length &&
    want.every((segment, index) => segment.startsWith(':') || segment === got[index])
  );
}

export function stubAccountFetch(routes: Readonly<Record<string, RouteHandler>>): AccountFetch {
  const calls: RecordedCall[] = [];
  const counts = new Map<string, number>();
  const mock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const raw = typeof input === 'string' ? input : input.toString();
    const url = new URL(raw, 'http://testserver');
    const method = init?.method ?? 'GET';
    const call: RecordedCall = {
      method,
      path: url.pathname,
      query: url.searchParams,
      authorization: headerOf(init, 'Authorization'),
      body: bodyOf(init),
    };
    calls.push(call);
    const key = Object.keys(routes).find((candidate) => {
      const [candidateMethod, candidatePath] = candidate.split(' ');
      return (
        candidateMethod === method &&
        candidatePath !== undefined &&
        matches(candidatePath, url.pathname)
      );
    });
    if (key === undefined) {
      if (url.pathname === '/health/ready') {
        return new Promise<Response>(() => undefined);
      }
      return Promise.reject(new Error(`unrouted fetch: ${method} ${url.pathname}${url.search}`));
    }
    const callNumber = (counts.get(key) ?? 0) + 1;
    counts.set(key, callNumber);
    const handler = routes[key];
    if (handler === undefined) {
      return Promise.reject(new Error(`no handler for ${key}`));
    }
    return Promise.resolve(handler(call, callNumber));
  });
  vi.stubGlobal('fetch', mock);
  return { calls, mock };
}

export function callsTo(fetch: AccountFetch, method: string, pathPattern: string): RecordedCall[] {
  return fetch.calls.filter((call) => call.method === method && matches(pathPattern, call.path));
}

export const ok = (body: unknown) => (): Response => jsonResponse(200, body);
export const status =
  (code: number, errorCode: string, message = 'fixture message', extra: object = {}) =>
  (): Response =>
    jsonResponse(code, { error: { code: errorCode, message, ...extra } });
export const hang = (): Promise<Response> => new Promise<Response>(() => undefined);

export const USER_A: User = {
  id: 'user-a',
  email: 'a@example.com',
  created_at: '2026-09-01T10:00:00Z',
};
export const USER_B: User = {
  id: 'user-b',
  email: 'b@example.com',
  created_at: '2026-09-01T10:00:00Z',
};

export function tokenFor(user: User): string {
  return `token-${user.id}`;
}

/** A fresh store, signed in as `user` with a 15-minute token — no refresh is needed in a test. */
export function signInAs(user: User): void {
  __resetForTests();
  authStore.setAuthenticated({
    access_token: tokenFor(user),
    token_type: 'Bearer',
    expires_in: 900,
    user,
  });
}

export function bearerFor(user: User): string {
  return `Bearer ${tokenFor(user)}`;
}

export function makeHistoryEntry(overrides: Partial<HistoryEntry> = {}): HistoryEntry {
  return {
    id: 'entry-1',
    status: 'succeeded',
    failure_reason: null,
    retryable: false,
    requested_at: '2026-09-20T10:00:00Z',
    completed_at: '2026-09-20T10:00:09Z',
    version: 3,
    edited: false,
    base_cv_id: 'cv-1',
    base_cv: { id: 'cv-1', label: 'Backend roles', original_filename: 'jane.pdf' },
    posting: {
      id: 'posting-1',
      source: 'pasted',
      title: 'Senior Platform Engineer',
      source_url: null,
      preview: 'We are looking for a platform engineer…',
    },
    ...overrides,
  };
}

export function historyPage(
  items: readonly HistoryEntry[],
  nextCursor: string | null = null,
): HistoryPage {
  return { items, next_cursor: nextCursor };
}

/** An account run: 1.4's shape, `expires_at: null` (kept until deleted — AC-34). */
export function makeAccountRun(overrides: Partial<TailoringRun> = {}): TailoringRun {
  return makeRun({ expires_at: null, ...overrides });
}

/** The routes every signed-in page needs: `/me` answers as `user`, the guest list is empty. */
export function signedInRoutes(user: User): Record<string, RouteHandler> {
  return {
    'GET /api/auth/me': ok(user),
    'GET /api/tailoring-runs': ok({ items: [] }),
    // Slice 2.4: the claim offer reads the guest CV list beside the run list, on `/` and on a
    // guest run page. Empty, so a test about something else sees no offer.
    'GET /api/base-cvs': ok({ items: [] }),
    // Slice 3.1: a succeeded history row and an account run page read the board for their
    // "On your board" badge. Empty, so a test about something else sees "Add to board" and nothing
    // else. A test about the board overrides it.
    'GET /api/me/board': ok({ items: [] }),
  };
}
