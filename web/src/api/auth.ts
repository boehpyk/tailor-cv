import { request } from './client';

import type { AuthenticatedResponse, Credentials, User } from '@/features/auth/types';

/**
 * The five `/api/auth/*` endpoints (technical plan §4) — transport only.
 *
 * **None of these touches the auth store.** They return what the server said and throw an
 * `ApiError` keyed by `code` when it refused; deciding what a response *means* for the session
 * (`AUTHENTICATED`, `SIGNED_OUT`, `unavailable`) is `features/auth/authStore.ts`'s job, one layer
 * up. That split is what lets the store call `refresh` without this module knowing the store
 * exists, and it keeps the dependency pointing one way: features → api, never back.
 *
 * **Cookies.** The refresh token is an `HttpOnly` cookie scoped to `Path=/api/auth` (ADR-0008,
 * AC-24). `request` already sends `credentials: 'include'` on every call, so the browser attaches
 * `tc_refresh` to these requests without anything here asking for it — and, because of the `Path`,
 * to no other request. JavaScript never reads or writes it.
 *
 * **Origin.** `register`, `login`, `refresh` and `logout` are refused without a trusted `Origin`
 * (technical plan §0.5). A browser sets that header on every same-origin `POST` by itself; it is a
 * forbidden header name, so nothing here could set it even if it tried.
 *
 * **Which of these carries the access token.** Only `me` — it is the one declared
 * `auth: 'required'`. `refresh` and `logout` must never be: `refresh` is what the interceptor calls
 * *when* the token is missing or stale, so routing it through the interceptor would recurse, and
 * `logout` must work precisely when the access token has expired (its cookie names the login).
 */

/**
 * Create an account and log it in: **201** with an `AuthenticatedResponse` and a fresh
 * `tc_refresh` cookie.
 *
 * Refusals (`ApiError.code`): 409 `email_already_registered`; 422 `invalid_email`,
 * `password_too_short` (`details.min_length`), `password_too_long` (`details.max_length`),
 * `password_matches_email`, `validation_error`; 429 `rate_limited` (`retryAfterSeconds`); 403
 * `origin_not_allowed`; 503 `rate_limit_unavailable` or `service_unavailable`.
 */
export function register(
  credentials: Credentials,
  signal?: AbortSignal,
): Promise<AuthenticatedResponse> {
  return request<AuthenticatedResponse>('/api/auth/register', {
    method: 'POST',
    body: credentials,
    ...(signal ? { signal } : {}),
  });
}

/**
 * Log in: **200** with an `AuthenticatedResponse` and a fresh `tc_refresh` cookie.
 *
 * An unknown email and a wrong password are the **same** 401 `invalid_credentials` — the server
 * makes them indistinguishable on purpose (AC-28), so the UI has exactly one sentence for both.
 * Otherwise: 422 `invalid_email` / `validation_error`; 429 `rate_limited`; 403
 * `origin_not_allowed`; 503 `rate_limit_unavailable` or `service_unavailable`.
 */
export function login(
  credentials: Credentials,
  signal?: AbortSignal,
): Promise<AuthenticatedResponse> {
  return request<AuthenticatedResponse>('/api/auth/login', {
    method: 'POST',
    body: credentials,
    ...(signal ? { signal } : {}),
  });
}

/**
 * Rotate the refresh cookie and mint a new access token: **200** `AuthenticatedResponse`.
 *
 * A `POST` with no body — the credential is the cookie. Refusals: 401 `not_signed_in` (no cookie,
 * or a login that no longer exists) and 401 `refresh_token_reused` (a replay; the server has revoked
 * the whole login); 409 `refresh_in_progress` (another tab won a race inside the server's 10 s
 * grace — wait and retry, the browser will hold the winner's cookie); 403 `origin_not_allowed`;
 * 503 `service_unavailable`.
 *
 * **Call this only through `authStore.refresh()`**, which makes it single-flight. Two concurrent
 * calls from one tab present the same cookie twice, and the second is exactly the replay the
 * server's grace window exists to tell apart from theft.
 */
export function refresh(signal?: AbortSignal): Promise<AuthenticatedResponse> {
  return request<AuthenticatedResponse>('/api/auth/refresh', {
    method: 'POST',
    ...(signal ? { signal } : {}),
  });
}

/**
 * End the login this browser's cookie names: **204** and a cleared cookie. Idempotent — with no
 * cookie it is still a 204.
 *
 * A 503 `service_unavailable` means the login was **not** ended and the cookie was **not**
 * cleared (I-31): the caller must keep the user logged in and say so, not pretend it worked.
 */
export async function logout(signal?: AbortSignal): Promise<void> {
  await request<null>('/api/auth/logout', {
    method: 'POST',
    ...(signal ? { signal } : {}),
  });
}

/**
 * The user the current access token belongs to — the one bearer-authenticated endpoint in 2.1.
 *
 * `auth: 'required'`: the client attaches `Authorization: Bearer …`, refreshing first when the
 * token is near expiry, and answers one 401 `invalid_access_token` with one refresh and one retry
 * (AC-38). A 401 `not_signed_in` means the user no longer exists; 503 `service_unavailable`.
 */
export function me(signal?: AbortSignal): Promise<User> {
  return request<User>('/api/auth/me', {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}
