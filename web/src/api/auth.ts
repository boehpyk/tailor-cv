import { request } from './client';

import type { AuthenticatedResponse, Credentials, User } from '@/features/auth/types';

/**
 * The `/api/auth/*` endpoints (2.1's five, plus 2.2's `delete-account`) — transport only.
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
 * **Which of these carries the access token.** `me` and `deleteAccount` — the two declared
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

/**
 * Delete the signed-in account (slice 2.2): **204** and a cleared `tc_refresh` cookie. Irreversible —
 * the account, every saved CV and its file, and every login on every device.
 *
 * `auth: 'required'` **and** the password: the bearer says who, the password says it is really them —
 * a borrowed, unlocked laptop must not be able to do this. A `POST` under `/api/auth` rather than a
 * `DELETE` on `/api/me`, because only a route under `Path=/api/auth` can clear `tc_refresh`, and a
 * body on a `DELETE` is dropped by enough intermediaries to be a bug waiting.
 *
 * **A wrong password is 403 `password_incorrect`, not 401** (AC-43): the requester *is* signed in,
 * so the interceptor — which refreshes only on 401 `invalid_access_token` — never refreshes on it.
 * Also: 403 `origin_not_allowed`; 401 `invalid_access_token` / `not_signed_in`; 422
 * `validation_error`; 429 `rate_limited`; 503 `rate_limit_unavailable` / `service_unavailable` —
 * and on any refusal **nothing was deleted**.
 *
 * Like the rest of this module, this does not touch the auth store: signing this tab out, clearing
 * the cache and telling other tabs is `useDeleteAccount`'s job.
 */
export async function deleteAccount(password: string, signal?: AbortSignal): Promise<void> {
  await request<null>('/api/auth/delete-account', {
    method: 'POST',
    body: { password },
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

// --- Slice 2.5: the email channel (technical plan §4, §7) -----------------------------------------
//
// Every one of these is a cookie-less, bearer-less `POST` under `/api/auth`: none of them signs
// anyone in, so there is no token to attach and none to receive. Each still needs the trusted
// `Origin`, which a same-origin `fetch` sends by itself (see the module docblock).
//
// **A token never travels in a URL the app builds.** It arrives in the link's *fragment* (which the
// browser never sends), is read once by `useFragmentToken`, and leaves again only here, in a JSON
// body (AC-51).

/**
 * Ask for an account: **202, empty**, whatever the address — a new one gets a confirmation mail, a
 * registered one gets a "you already have an account" mail, and the response cannot tell the two
 * apart (ADR-0008 (h)). No cookie, no token: registering no longer signs in.
 *
 * Refusals: 422 `invalid_email`, `password_too_short` (`details.min_length`), `password_too_long`
 * (`details.max_length`), `password_matches_email`, `validation_error`; 429 `rate_limited`
 * (`retryAfterSeconds`); 403 `origin_not_allowed`; 503 `rate_limit_unavailable` /
 * `service_unavailable`. `email_already_registered` is no longer possible here.
 */
export async function requestRegistration(
  credentials: Credentials,
  signal?: AbortSignal,
): Promise<void> {
  await request<null>('/api/auth/register', {
    method: 'POST',
    body: credentials,
    ...(signal ? { signal } : {}),
  });
}

/**
 * Turn a pending registration into an account: **204**. Does **not** sign in (plan §0.6) — the
 * page offers **Log in**.
 *
 * Refusals: 400 `link_invalid` (expired, already used, or never issued — one code on purpose);
 * 409 `email_already_registered` (the address became an account meanwhile); 422
 * `validation_error`; 403 `origin_not_allowed`; 503 `service_unavailable`.
 */
export async function confirmRegistration(token: string, signal?: AbortSignal): Promise<void> {
  await request<null>('/api/auth/registration/confirm', {
    method: 'POST',
    body: { token },
    ...(signal ? { signal } : {}),
  });
}

/**
 * Ask for a password-reset link: **202, empty**, whether or not the address has an account — the
 * answer is the same either way (no enumeration).
 *
 * Refusals: 422 `invalid_email` / `validation_error`; 429 `rate_limited` (`retryAfterSeconds`);
 * 403 `origin_not_allowed`; 503 `rate_limit_unavailable` / `service_unavailable`.
 */
export async function requestPasswordReset(email: string, signal?: AbortSignal): Promise<void> {
  await request<null>('/api/auth/password-reset', {
    method: 'POST',
    body: { email },
    ...(signal ? { signal } : {}),
  });
}

/** What `confirmPasswordReset` sends: the token from the link and the new password. */
export interface PasswordResetConfirmation {
  readonly token: string;
  readonly password: string;
}

/**
 * Set a new password with a reset link's token: **204**. Every login of the account is revoked on
 * the server (ADR-0028), this browser's included — the caller signs the tab out with reason
 * `password_changed`; this function, like the rest of the module, does not touch the auth store.
 *
 * Refusals: 400 `link_invalid`; 422 `password_too_short` / `password_too_long` (with the server's
 * bound in `details`) / `password_matches_email` / `validation_error` — the token is **not**
 * consumed by a 422, so the same link can try again; 403 `origin_not_allowed`; 503
 * `service_unavailable`.
 */
export async function confirmPasswordReset(
  confirmation: PasswordResetConfirmation,
  signal?: AbortSignal,
): Promise<void> {
  await request<null>('/api/auth/password-reset/confirm', {
    method: 'POST',
    body: { token: confirmation.token, password: confirmation.password },
    ...(signal ? { signal } : {}),
  });
}
