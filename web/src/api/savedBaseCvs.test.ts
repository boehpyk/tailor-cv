import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests, authStore } from '@/features/auth/authStore';

import { copySavedBaseCv, fetchBaseCvs, uploadBaseCv } from './baseCvs';
import { deleteAccount } from './auth';
import {
  deleteSavedBaseCv,
  fetchSavedBaseCvs,
  renameSavedBaseCv,
  uploadSavedBaseCv,
} from './savedBaseCvs';

/**
 * T26 RED (against the spec) — AC-43, against feature-spec.md and technical-plan.md §7/§4.
 *
 * **Written against already-complete code, not a skeleton.** `api/savedBaseCvs.ts`, `api/baseCvs.ts`
 * and `api/auth.ts` are T24's typed client (real, non-stub) and `api/client.ts`'s `auth: 'required'`
 * interceptor predates this slice entirely — so every assertion below is expected to **pass on this
 * first run**. That is not a defect in the test: T26's RED tier exists to catch a skeleton that
 * cannot yet discriminate right from wrong, and there is no skeleton here to be red against. This
 * file is the structural half of AC-43 — it pins behaviour T24 already delivers so a future change
 * to `api/client.ts`'s auth wiring (adding the bearer to a guest route, or teaching the interceptor
 * to refresh on a code besides `invalid_access_token`) turns it red instead of shipping silently.
 */

const TOKEN_RESPONSE = {
  access_token: 'token-abc',
  token_type: 'Bearer' as const,
  expires_in: 900,
  user: { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-25T10:00:00Z' },
};

function authHeaderOf(init: RequestInit | undefined): string | undefined {
  const headers = init?.headers as Record<string, string> | undefined;
  return headers?.Authorization;
}

/** A `fetch` stub with typed parameters, so `.mock.calls[n]` is `[string | URL, RequestInit?]` —
 * an untyped `vi.fn(() => …)` infers zero parameters and every call tuple as `[]`. Also not typed
 * via `ReturnType<typeof vi.fn>` (unparameterized): that widens the concrete mock back to the
 * default `(...args: any[]) => any` shape, which is what turned `.mock.calls[n][0].toString()`
 * into an unsafe-`any` access below. */
function stubFetch(respond: () => Response | Promise<Response>) {
  const mock = vi.fn<(input: string | URL, init?: RequestInit) => Promise<Response>>(() =>
    Promise.resolve(respond()),
  );
  vi.stubGlobal('fetch', mock);
  return mock;
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('AC-43: every /api/me/* call, and the copy, carry the bearer', () => {
  it('fetchSavedBaseCvs sends Authorization: Bearer <token>', async () => {
    authStore.setAuthenticated(TOKEN_RESPONSE);
    const fetchMock = stubFetch(() => jsonResponse(200, { items: [] }));

    await fetchSavedBaseCvs();

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(authHeaderOf(init)).toBe('Bearer token-abc');
  });

  it('uploadSavedBaseCv sends Authorization: Bearer <token>', async () => {
    authStore.setAuthenticated(TOKEN_RESPONSE);
    const fetchMock = stubFetch(() =>
      jsonResponse(201, {
        id: 'cv-1',
        label: null,
        original_filename: 'a.pdf',
        content_type: 'application/pdf',
        size_bytes: 10,
        status: 'extracted',
        character_count: 1,
        failure_reason: null,
        failure_message: null,
        uploaded_at: '2026-09-25T10:00:00Z',
      }),
    );

    await uploadSavedBaseCv(new File(['x'], 'a.pdf', { type: 'application/pdf' }));

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(authHeaderOf(init)).toBe('Bearer token-abc');
  });

  it('renameSavedBaseCv sends Authorization: Bearer <token>', async () => {
    authStore.setAuthenticated(TOKEN_RESPONSE);
    const fetchMock = stubFetch(() =>
      jsonResponse(200, {
        id: 'cv-1',
        label: 'x',
        original_filename: 'a.pdf',
        content_type: 'application/pdf',
        size_bytes: 10,
        status: 'extracted',
        character_count: 1,
        failure_reason: null,
        failure_message: null,
        uploaded_at: '2026-09-25T10:00:00Z',
      }),
    );

    await renameSavedBaseCv('cv-1', 'x');

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(authHeaderOf(init)).toBe('Bearer token-abc');
  });

  it('deleteSavedBaseCv sends Authorization: Bearer <token>', async () => {
    authStore.setAuthenticated(TOKEN_RESPONSE);
    const fetchMock = stubFetch(() => new Response(null, { status: 204 }));

    await deleteSavedBaseCv('cv-1');

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(authHeaderOf(init)).toBe('Bearer token-abc');
  });

  it('copySavedBaseCv (the transfer route) sends Authorization: Bearer <token> AND credentials: include', async () => {
    authStore.setAuthenticated(TOKEN_RESPONSE);
    const fetchMock = stubFetch(() =>
      jsonResponse(201, {
        id: 'guest-cv-1',
        original_filename: 'a.pdf',
        content_type: 'application/pdf',
        size_bytes: 10,
        status: 'extracted',
        character_count: 1,
        failure_reason: null,
        failure_message: null,
        uploaded_at: '2026-09-25T10:00:00Z',
        expires_at: '2026-09-26T10:00:00Z',
        origin: 'copied_from_saved',
      }),
    );

    await copySavedBaseCv('saved-cv-1');

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(authHeaderOf(init)).toBe('Bearer token-abc');
    expect(init?.credentials).toBe('include');
  });
});

describe('AC-43: guest routes never carry the bearer, even while signed in', () => {
  it('fetchBaseCvs sends no Authorization header while authenticated', async () => {
    authStore.setAuthenticated(TOKEN_RESPONSE);
    const fetchMock = stubFetch(() => jsonResponse(200, { items: [] }));

    await fetchBaseCvs();

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(authHeaderOf(init)).toBeUndefined();
  });

  it('uploadBaseCv sends no Authorization header while authenticated', async () => {
    authStore.setAuthenticated(TOKEN_RESPONSE);
    const fetchMock = stubFetch(() =>
      jsonResponse(201, {
        id: 'guest-cv-1',
        original_filename: 'a.pdf',
        content_type: 'application/pdf',
        size_bytes: 10,
        status: 'extracted',
        character_count: 1,
        failure_reason: null,
        failure_message: null,
        uploaded_at: '2026-09-25T10:00:00Z',
        expires_at: '2026-09-26T10:00:00Z',
      }),
    );

    await uploadBaseCv(new File(['x'], 'a.pdf', { type: 'application/pdf' }));

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(authHeaderOf(init)).toBeUndefined();
  });
});

describe('AC-43: a 403 password_incorrect triggers no refresh', () => {
  it('deleteAccount does not call POST /api/auth/refresh when the password is wrong', async () => {
    authStore.setAuthenticated(TOKEN_RESPONSE);
    const fetchMock = vi.fn((input: string | URL): Promise<Response> => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/auth/delete-account') {
        return Promise.resolve(
          jsonResponse(403, { error: { code: 'password_incorrect', message: 'nope' } }),
        );
      }
      return Promise.reject(new Error(`unexpected fetch: ${url}`));
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(deleteAccount('wrong-password')).rejects.toThrow();

    expect(
      fetchMock.mock.calls.some(([input]) => {
        const url = typeof input === 'string' ? input : input.toString();
        return url === '/api/auth/refresh';
      }),
    ).toBe(false);
  });
});
