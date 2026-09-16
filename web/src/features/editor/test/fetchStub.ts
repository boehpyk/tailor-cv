import { vi } from 'vitest';

import type { TailoredDocumentKind } from '@/features/tailoring/types';

/**
 * A `fetch` stub for the editor's own endpoints — `PUT
 * /api/tailoring-runs/{id}/documents/{kind}` and the run's `GET` (needed by the 409 same-content
 * recovery, which refetches) — generalising `@/test/fixtures.ts`'s `stubWorkspaceFetch` pattern to
 * the two routes that module does not cover. Kept local to the editor feature (rather than added to
 * the shared helper) because `qa` does not modify production or shared test infrastructure it did
 * not write for this slice; the routing, error-on-unhandled-call and per-key call counting follow
 * `stubWorkspaceFetch` exactly, for the same reasons given there.
 */
export interface DocumentFetchStubs {
  readonly runDetail?: (callNumber: number) => Promise<Response>;
  readonly putDocument?: Partial<
    Record<TailoredDocumentKind, (callNumber: number) => Promise<Response>>
  >;
}

export function stubDocumentFetch(stubs: DocumentFetchStubs): ReturnType<typeof vi.fn> {
  const putCallCounts = new Map<TailoredDocumentKind, number>();
  let runDetailCalls = 0;

  const fetchMock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';

    const putMatch = /^\/api\/tailoring-runs\/([^/]+)\/documents\/([^/]+)$/.exec(url);
    if (putMatch && method === 'PUT') {
      const kind = decodeURIComponent(putMatch[2] ?? '') as TailoredDocumentKind;
      const handler = stubs.putDocument?.[kind];
      if (handler === undefined) {
        return Promise.reject(new Error(`unexpected PUT ${url} in this test`));
      }
      const callNumber = (putCallCounts.get(kind) ?? 0) + 1;
      putCallCounts.set(kind, callNumber);
      return handler(callNumber);
    }

    const detailMatch = /^\/api\/tailoring-runs\/([^/]+)$/.exec(url);
    if (detailMatch && method === 'GET') {
      if (stubs.runDetail === undefined) {
        return Promise.reject(new Error(`unexpected GET ${url} in this test`));
      }
      runDetailCalls += 1;
      return stubs.runDetail(runDetailCalls);
    }

    return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
  });

  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/**
 * `client.ts`'s `request()` always sends a JSON body as a plain string (`JSON.stringify(...)`,
 * never `FormData` for this endpoint) — narrowed explicitly rather than `String(init?.body)`, which
 * would silently stringify an unexpected `BodyInit` to `"[object Object]"` and fail the `JSON.parse`
 * with a confusing error far from the real cause.
 */
function bodyText(init: RequestInit | undefined, path: string): string {
  const { body } = init ?? {};
  if (typeof body !== 'string') {
    throw new Error(`expected a JSON string body for ${path}, got ${typeof body}`);
  }
  return body;
}

/**
 * The JSON body of the most recent `PUT` recorded against `path` — for asserting on
 * `expected_version` (AC-31, AC-32) without hand-parsing `RequestInit` at every call site.
 * Throws if there is none, so a test that expected a save and got none fails loudly rather than on
 * `undefined.expected_version`.
 */
export function lastPutBody(fetchMock: ReturnType<typeof vi.fn>, path: string): unknown {
  const puts = fetchMock.mock.calls.filter((call) => {
    const [input, init] = call as [unknown, RequestInit | undefined];
    const url = typeof input === 'string' ? input : String(input);
    return url === path && (init?.method ?? 'GET') === 'PUT';
  });
  const last = puts[puts.length - 1] as [unknown, RequestInit | undefined] | undefined;
  if (last === undefined) {
    throw new Error(`no PUT was recorded for ${path}`);
  }
  const [, init] = last;
  return JSON.parse(bodyText(init, path));
}

/** Every `PUT` body recorded against `path`, in call order — for AC-32's ordering assertion. */
export function allPutBodies(fetchMock: ReturnType<typeof vi.fn>, path: string): unknown[] {
  return fetchMock.mock.calls
    .filter((call) => {
      const [input, init] = call as [unknown, RequestInit | undefined];
      const url = typeof input === 'string' ? input : String(input);
      return url === path && (init?.method ?? 'GET') === 'PUT';
    })
    .map((call) => {
      const [, init] = call as [unknown, RequestInit | undefined];
      return JSON.parse(bodyText(init, path)) as unknown;
    });
}
