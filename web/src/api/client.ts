/**
 * The typed API client — the only place in this application that calls `fetch`.
 *
 * That rule is what keeps components ignorant of HTTP. A component calls a hook, the hook calls a
 * function in `src/api/`, and this module is the only thing that knows about status codes, headers
 * and JSON. Change the transport and exactly one directory changes.
 */

/** An error the API reported, carrying the status so a caller can distinguish 4xx from 5xx. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

interface RequestOptions {
  readonly signal?: AbortSignal;
  readonly method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  readonly body?: unknown;
  /**
   * Statuses that carry a meaningful body and must NOT be turned into a thrown error.
   *
   * `/health/ready` is the reason this exists: it answers 503 *with the full report of which
   * dependency is down*. Throwing there would discard the only information the caller wanted and
   * replace it with "request failed" — the failure is the payload.
   */
  readonly acceptStatuses?: readonly number[];
}

/**
 * Perform a request and parse the JSON body.
 *
 * `credentials: 'include'` because the refresh token is an HttpOnly cookie (ADR-0008) — the access
 * token lives in memory and is never in localStorage, which matters more here than in most apps:
 * this one renders model-generated content into a rich text editor, so an XSS assumption should not
 * be the thing standing between an attacker and a session.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    credentials: 'include',
    headers: options.body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    ...(options.signal ? { signal: options.signal } : {}),
  });

  // Some endpoints answer with a body on failure and some do not, so read defensively rather than
  // letting a `.json()` on an empty 503 throw a parse error that hides the real status.
  const text = await response.text();
  const parsed: unknown = text.length > 0 ? JSON.parse(text) : null;

  const accepted = response.ok || (options.acceptStatuses?.includes(response.status) ?? false);
  if (!accepted) {
    throw new ApiError(response.status, `${String(response.status)} for ${path}`);
  }

  return parsed as T;
}
