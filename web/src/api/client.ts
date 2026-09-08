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
    /**
     * The server's stable machine-readable code (e.g. `guest_session_expired`,
     * `unsupported_format`) when the body was the `{"error": {"code", "message"}}` envelope every
     * TailorCraft error response uses — `null` otherwise (a network failure, or an endpoint that
     * predates the envelope, like `/health/ready`'s bare report).
     *
     * A caller branches on `code`, never on `message`: `message` is prose for a human and can
     * change wording without notice, `code` is the contract (schemas/intake.py's `ErrorDetail`).
     */
    readonly code: string | null,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** The `{"error": {"code": "...", "message": "..."}}` shape every TailorCraft error response uses. */
interface ErrorEnvelope {
  readonly error: { readonly code: string; readonly message: string };
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== 'object' || value === null || !('error' in value)) {
    return false;
  }
  const inner: unknown = value.error;
  if (typeof inner !== 'object' || inner === null) {
    return false;
  }
  return (
    typeof (inner as { code: unknown }).code === 'string' &&
    typeof (inner as { message: unknown }).message === 'string'
  );
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
  // A multipart upload (`FormData`) must NOT get a hand-set `Content-Type` and must NOT be run
  // through `JSON.stringify` — this looks wrong until you know why. `multipart/form-data` requires
  // a `boundary` parameter that only the browser's own `fetch` implementation can generate (it is
  // derived per-request), and setting the header yourself produces a `Content-Type` with no
  // boundary at all: the server can see the request is multipart but can never find where one part
  // ends and the next begins, so parsing fails on every upload. Leaving `headers` and `body` alone
  // for `FormData` lets the browser set its own `Content-Type: multipart/form-data; boundary=...`.
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    credentials: 'include',
    headers:
      options.body === undefined || options.body instanceof FormData
        ? undefined
        : { 'Content-Type': 'application/json' },
    body:
      options.body === undefined
        ? undefined
        : options.body instanceof FormData
          ? options.body
          : JSON.stringify(options.body),
    ...(options.signal ? { signal: options.signal } : {}),
  });

  // Some endpoints answer with a body on failure and some do not, so read defensively rather than
  // letting a `.json()` on an empty 503 throw a parse error that hides the real status.
  const text = await response.text();
  const parsed: unknown = text.length > 0 ? JSON.parse(text) : null;

  const accepted = response.ok || (options.acceptStatuses?.includes(response.status) ?? false);
  if (!accepted) {
    // Prefer the server's own code and message (the `{"error": {...}}` envelope) when the body has
    // that shape; fall back to a synthesized message for endpoints that predate it (`/health/ready`)
    // or a transport-level failure with no parseable body at all.
    if (isErrorEnvelope(parsed)) {
      throw new ApiError(response.status, parsed.error.message, parsed.error.code);
    }
    throw new ApiError(response.status, `${String(response.status)} for ${path}`, null);
  }

  return parsed as T;
}
