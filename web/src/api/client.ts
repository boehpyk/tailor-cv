/**
 * The typed API client — the only place in this application that calls `fetch`.
 *
 * That rule is what keeps components ignorant of HTTP. A component calls a hook, the hook calls a
 * function in `src/api/`, and this module is the only thing that knows about status codes, headers
 * and JSON. Change the transport and exactly one directory changes.
 */

// A cycle, on purpose and safe: `authStore` → `api/auth` → this module → `authStore`. Nothing here
// touches `authStore` at module top level — only inside `requestWithAuth` — so the live binding is
// always initialised by the time it is read. See the note in `authStore.ts`.
import { authStore } from '@/features/auth/authStore';

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
    /**
     * Every key the error envelope carried **besides** `code` and `message` — `{}` when there were
     * none, or when the body was not the envelope at all.
     *
     * Most rejections carry only a reason, but some carry a fact the client is meant to act on: a
     * 409 `tailoring_already_running` names the run already in flight (`active_tailoring_run_id`,
     * AC-17) so the UI can attach to it instead of paying for a second one. Keeping only `code` and
     * `message` made that fact unreachable, and a spec that put the id in the body *so the client
     * could use it* was quietly defeated one layer down.
     *
     * The values are `unknown`, not a typed shape, on purpose. The client does not know every
     * code's extra keys, so the reader that knows what a key means narrows it at the point of use
     * (`typeof id === 'string'`) rather than this class asserting a shape the server never promised.
     *
     * Defaulted, so every existing `new ApiError(status, message, code)` is unchanged.
     */
    readonly details: Readonly<Record<string, unknown>> = {},
    /**
     * The `Retry-After` header as a number of seconds, when a response carried one in its
     * delta-seconds form — the form every TailorCraft 429 uses — and `null` otherwise (no header,
     * or the HTTP-date form, which nothing here sends and nothing here parses).
     *
     * A header, not a body field, so it is read in this module and nowhere else: `client.ts` is
     * the one place that knows about headers, and a hook that wanted this number without it would
     * either make one up or scrape the server's prose `message`, which is not a contract.
     *
     * Defaulted, so every existing `new ApiError(status, message, code)` is unchanged.
     */
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** `Retry-After: 37` → 37; anything else (absent, an HTTP-date, garbage) → `null`. */
function retryAfterSecondsOf(response: Response): number | null {
  const header = response.headers.get('Retry-After');
  if (header === null || !/^\d+$/.test(header.trim())) {
    return null;
  }
  return Number(header.trim());
}

/**
 * The `{"error": {"code": "...", "message": "...", ...}}` shape every TailorCraft error response
 * uses. `code` and `message` are always there; the index signature is the room for the extra keys a
 * particular code adds (see `ApiError.details`).
 */
interface ErrorEnvelope {
  readonly error: {
    readonly code: string;
    readonly message: string;
    readonly [key: string]: unknown;
  };
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

/**
 * Turn a non-2xx `Response` and its already-read body into the `ApiError` a caller will see.
 *
 * Extracted so that `request` and `requestBlob` cannot disagree about what a failure *is*. They
 * differ only in how they read a **success** — JSON versus bytes — and a second hand-written copy
 * of the envelope handling in the blob path is exactly how a 409 `export_not_ready` would arrive
 * with `code: null` on one path and `code: 'export_not_ready'` on the other, which is the fact the
 * export bar branches on.
 *
 * The `parsed` argument is whatever the body turned out to be (`null` when there was none or it
 * would not parse), never the raw text: the envelope check is a shape check, and doing it here
 * keeps `isErrorEnvelope` the single definition of that shape.
 */
function apiErrorFor(response: Response, path: string, parsed: unknown): ApiError {
  // Prefer the server's own code and message (the `{"error": {...}}` envelope) when the body has
  // that shape; fall back to a synthesized message for endpoints that predate it (`/health/ready`)
  // or a transport-level failure with no parseable body at all.
  if (isErrorEnvelope(parsed)) {
    // The rest of the envelope is kept, not dropped: some codes carry a fact the caller acts on
    // (a 409 `tailoring_already_running` names the active run). See `ApiError.details`.
    const { code, message, ...details } = parsed.error;
    return new ApiError(response.status, message, code, details, retryAfterSecondsOf(response));
  }
  return new ApiError(
    response.status,
    `${String(response.status)} for ${path}`,
    null,
    {},
    retryAfterSecondsOf(response),
  );
}

interface RequestOptions {
  readonly signal?: AbortSignal;
  readonly method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  readonly body?: unknown;
  /**
   * Statuses that carry a meaningful body and must NOT be turned into a thrown error.
   *
   * `/health/ready` is the reason this exists: it answers 503 *with the full report of which
   * dependency is down*. Throwing there would discard the only information the caller wanted and
   * replace it with "request failed" — the failure is the payload.
   */
  readonly acceptStatuses?: readonly number[];
  /**
   * `'required'` makes this a **bearer-authenticated** request (slice 2.1, AC-38). Absent — the
   * default, and every guest endpoint — and the request carries no `Authorization` header at all.
   *
   * Opt-in rather than "attach the token whenever there is one", for two reasons:
   *
   * - **No request carries two credentials by accident.** Guest endpoints are authorized by the
   *   `tc_guest` cookie; a guest request that also carried a bearer token would be the first step
   *   of the claim (slice 2.4) happening without anyone designing it.
   * - **The retry is keyed on the declaration.** A 401 whose `code` is `invalid_access_token`, on
   *   a request declared `'required'`, gets one `authStore.refresh()` and one retry. A 401
   *   `guest_session_expired` never refreshes (I-48): the branch is on `code`, never on status,
   *   because "401" alone means different things on different routes.
   *
   * Before sending, the token comes from `authStore.accessTokenForRequest()`, which refreshes
   * *first* when less than 30 s remain. `api/auth.ts`'s `refresh` and `logout` must never set this:
   * the first is what the interceptor calls, and the second must work with an expired token.
   */
  readonly auth?: 'required';
}

/**
 * The `auth: 'required'` path: token → request → on 401 `invalid_access_token`, one refresh and one
 * retry → give up.
 *
 * **Branch on `code`, never on status.** A 401 on a guest route is `guest_session_expired`, and
 * refreshing a *login* cannot fix a *guest session* (I-48); a second 401 after the retry is
 * surfaced as-is, because a loop of refreshes is the one thing worse than an error.
 *
 * **Refresh only if the token we sent is still the current one.** Three requests that all went out
 * with the same stale token all come back 401; the first to land refreshes, and the others must
 * retry with the token that refresh produced rather than rotate the cookie again. The store's
 * single-flight covers the ones that overlap the refresh; this comparison covers the ones that land
 * after it finished.
 */
async function requestWithAuth<T>(path: string, options: RequestOptions): Promise<T> {
  const sentToken = await authStore.accessTokenForRequest();
  try {
    return await sendWithBearer<T>(path, options, sentToken);
  } catch (error) {
    // No token sent means nothing to refresh: `accessTokenForRequest` already gave the store its
    // chance, and the server's answer is the truthful one to surface.
    if (
      sentToken === null ||
      !(error instanceof ApiError) ||
      error.code !== 'invalid_access_token'
    ) {
      throw error;
    }
    let retryToken = await authStore.accessTokenForRequest();
    if (retryToken === null || retryToken === sentToken) {
      await authStore.refresh();
      retryToken = await authStore.accessTokenForRequest();
    }
    if (retryToken === null || retryToken === sentToken) {
      // The refresh did not produce a new token (logged out, or unavailable): the store has
      // already said so to React. The original refusal is the honest answer to this request.
      throw error;
    }
    return sendWithBearer<T>(path, options, retryToken);
  }
}

/**
 * `send`, plus the one answer to a bearer request that is about the **login** rather than the
 * request: 401 `not_signed_in` means the token verified but the user behind it no longer exists
 * (/verify round 1, finding 2). Refreshing cannot fix that, and neither can asking again, so the
 * store is told — `SIGNED_OUT` reason `expired`, which sends `RequireAuth` to `/login` — instead of
 * leaving the tab `authenticated` with a query that fails the same way on every Retry.
 *
 * The error is still thrown: this request failed, and its caller should see why. The store only
 * acts if it still holds `bearer` (see `signOutIfHolding`), so a late answer about an older login
 * cannot end a newer one.
 */
async function sendWithBearer<T>(
  path: string,
  options: RequestOptions,
  bearer: string | null,
): Promise<T> {
  try {
    return await send<T>(path, options, bearer);
  } catch (error) {
    if (bearer !== null && error instanceof ApiError && error.code === 'not_signed_in') {
      authStore.signOutIfHolding(bearer);
    }
    throw error;
  }
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
  if (options.auth === 'required') {
    return requestWithAuth<T>(path, options);
  }
  return send<T>(path, options, null);
}

/** Headers for one request: JSON's `Content-Type` when there is a JSON body, and the bearer. */
function headersFor(options: RequestOptions, bearer: string | null): Record<string, string> {
  const headers: Record<string, string> = {};
  // A multipart upload (`FormData`) must NOT get a hand-set `Content-Type` — this looks wrong until
  // you know why. `multipart/form-data` requires a `boundary` parameter that only the browser's own
  // `fetch` implementation can generate (it is derived per-request), and setting the header yourself
  // produces a `Content-Type` with no boundary at all: the server can see the request is multipart
  // but can never find where one part ends and the next begins, so parsing fails on every upload.
  if (options.body !== undefined && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }
  if (bearer !== null) {
    headers.Authorization = `Bearer ${bearer}`;
  }
  return headers;
}

/** One `fetch`, one parse, one verdict. `bearer` is `null` for every request not `auth: 'required'`. */
async function send<T>(path: string, options: RequestOptions, bearer: string | null): Promise<T> {
  const headers = headersFor(options, bearer);
  // A `FormData` body is passed through untouched rather than `JSON.stringify`-ed (see
  // `headersFor`). Spread rather than `headers: undefined`: under `exactOptionalPropertyTypes` an
  // optional `RequestInit` field may be absent but not explicitly `undefined`.
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    credentials: 'include',
    ...(Object.keys(headers).length === 0 ? {} : { headers }),
    ...(options.body === undefined
      ? {}
      : { body: options.body instanceof FormData ? options.body : JSON.stringify(options.body) }),
    ...(options.signal ? { signal: options.signal } : {}),
  });

  // Some endpoints answer with a body on failure and some do not, so read defensively rather than
  // letting a `.json()` on an empty 503 throw a parse error that hides the real status.
  const text = await response.text();
  const parsed: unknown = text.length > 0 ? JSON.parse(text) : null;

  const accepted = response.ok || (options.acceptStatuses?.includes(response.status) ?? false);
  if (!accepted) {
    throw apiErrorFor(response, path, parsed);
  }

  return parsed as T;
}

/** What a blob request may carry. A download is a `GET` with no body — there is nothing else. */
interface BlobRequestOptions {
  readonly signal?: AbortSignal;
}

/**
 * Perform a `GET` whose **success** is bytes, not JSON, and whose **failure** is JSON like every
 * other endpoint's.
 *
 * That asymmetry is the whole reason this function exists beside `request`. TailorCraft's two
 * download endpoints — the inline render and an export job's file — answer `200` with a
 * `Content-Type` of `application/pdf` or `text/markdown` and a `Content-Disposition`, and answer
 * every rejection with the ordinary `{"error": {"code", "message"}}` envelope. A single function
 * cannot parse both, and a caller must not have to know which it got: it gets a `Blob` or it gets
 * an `ApiError` with a `code`, exactly as it would from `request`.
 *
 * **This is why there is no `<a href="/api/…" download>` anywhere in the app** (AC-42). An anchor
 * has no error path: the browser follows it, the server answers 401 `guest_session_expired`, and
 * the user's Downloads folder receives a 60-byte JSON document named `tailored-cv.pdf`. A failure
 * that a user only discovers by opening the file is worse than one the UI states. Routing the
 * bytes through `fetch` costs an object URL and buys a real rejection.
 *
 * **The `Content-Disposition` filename is not read here.** Parsing it would mean re-implementing
 * RFC 6266 (quoting, `filename*`, encodings) to recover a value the client can name for itself
 * from `(document, format)` — and the server's is a constant, never the user's text. The caller
 * passes the filename to `saveBlob`.
 *
 * The response body is **read once**: `response.blob()` on success, `response.text()` on failure.
 * A rejection body is normally the envelope, but a proxy's 502 is HTML and a cut connection is
 * nothing at all, so the parse is guarded and falls through to the synthesized message rather than
 * replacing the server's status with a `SyntaxError`.
 */
export async function requestBlob(path: string, options: BlobRequestOptions = {}): Promise<Blob> {
  // `credentials: 'include'` for `request`'s reason: the refresh token is an HttpOnly cookie
  // (ADR-0008), and the guest session that authorizes this download is a cookie too.
  const response = await fetch(path, {
    method: 'GET',
    credentials: 'include',
    ...(options.signal ? { signal: options.signal } : {}),
  });

  if (!response.ok) {
    const text = await response.text();
    let parsed: unknown = null;
    if (text.length > 0) {
      try {
        parsed = JSON.parse(text);
      } catch {
        // Not JSON — a proxy's error page, say. `apiErrorFor` synthesizes a message from the status.
        parsed = null;
      }
    }
    throw apiErrorFor(response, path, parsed);
  }

  return response.blob();
}
