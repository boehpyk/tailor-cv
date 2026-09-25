/**
 * The wire shapes of `/api/auth/*` (technical plan §4), mirrored by hand like every other
 * `features/<slice>/types.ts`.
 *
 * Read-only on purpose: these are what the server said, and nothing in the client edits a response.
 */

/** A registered user, as `GET /api/auth/me` and every token response describe them. */
export interface User {
  readonly id: string;
  readonly email: string;
  /** ISO-8601, UTC, whole seconds. */
  readonly created_at: string;
}

/**
 * The body of a successful `register`, `login` or `refresh`.
 *
 * `expires_in` is **seconds from now**, not an instant: the client turns it into a deadline on its
 * own monotonic clock (`performance.now()`), so neither the server's clock nor the user's wall
 * clock is ever compared against it (AC-38). The token itself is opaque here — the client never
 * decodes the JWT, because nothing it could read there is something it is allowed to act on.
 */
export interface AuthenticatedResponse {
  readonly access_token: string;
  readonly token_type: 'Bearer';
  readonly expires_in: number;
  readonly user: User;
}

/** What `register` and `login` send. Typed input only; the API is the authority on every rule. */
export interface Credentials {
  readonly email: string;
  readonly password: string;
}

/**
 * Every `code` an `/api/auth/*` endpoint can answer with (technical plan §4's contract table).
 *
 * The set is closed *for these endpoints*, which is what lets `authCopy.ts` be exhaustive over it.
 * `ApiError.code` stays `string | null` — the client cannot promise a server never grows a code —
 * so narrow with `isAuthErrorCode` at the point of use rather than casting.
 */
export const AUTH_ERROR_CODES = [
  'origin_not_allowed',
  'invalid_email',
  'password_too_short',
  'password_too_long',
  'password_matches_email',
  'validation_error',
  'email_already_registered',
  'invalid_credentials',
  'rate_limited',
  'rate_limit_unavailable',
  'service_unavailable',
  'not_signed_in',
  'refresh_token_reused',
  'refresh_in_progress',
  'invalid_access_token',
] as const;

export type AuthErrorCode = (typeof AUTH_ERROR_CODES)[number];

const AUTH_ERROR_CODE_SET: ReadonlySet<string> = new Set(AUTH_ERROR_CODES);

export function isAuthErrorCode(code: string | null): code is AuthErrorCode {
  return code !== null && AUTH_ERROR_CODE_SET.has(code);
}
