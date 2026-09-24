/**
 * The one holder of the access token (AC-35; technical plan §0.6, §7).
 *
 * **Why a module and not React state or the query cache.** An access token is a *credential*, not
 * server state: it must never be refetched on focus, retried on its own or garbage-collected on a
 * timer — and it must be readable by `api/client.ts`, which is not a component. So it lives here,
 * at module scope, and React reads a token-free view of it through `useSyncExternalStore`
 * (`subscribe` + `getSnapshot`) — the hook React ships for exactly this. The user profile, which
 * *is* server state, lives only in TanStack Query under `['auth', 'me']`; this module never holds
 * it, it only hands the one from a token response back to whoever asked (`RefreshResult.user`) so
 * the caller can seed the cache.
 *
 * **What lives here:** the `AuthState` (the reducer in `authMachine.ts` owns every transition —
 * this module never assigns a status by hand), the boot promise, and the single in-flight refresh
 * promise. Nothing is persisted: not `localStorage`, not `sessionStorage`, not a readable cookie
 * (ADR-0008). A reload starts at `booting` and asks the server, via the `HttpOnly` refresh cookie.
 *
 * **Single-flight.** Every path that wants a refresh — the boot, `accessTokenForRequest` near
 * expiry, the interceptor's one retry on 401 `invalid_access_token`, the Retry button — gets the
 * *same* promise while one is out. Two refreshes from one tab would present one cookie twice, which
 * the server's grace answers with 409 `refresh_in_progress`: the tab racing itself (AC-36).
 *
 * **The clock is `performance.now()`**, read at call time — never `Date.now()`. `expiresAt` is
 * `performance.now() + expires_in * 1000`, so a user changing the system clock, or a laptop waking
 * with a skewed one, changes nothing (AC-38).
 *
 * ---
 *
 * **Test seams** (for `qa`; nothing in production code calls these):
 *
 * - **`fetch`** — this module reaches the network only through `api/auth.ts` → `api/client.ts` →
 *   the global `fetch`, so a `vi.stubGlobal('fetch', …)` / `vi.spyOn(globalThis, 'fetch')` stub
 *   sees every refresh, and counts them.
 * - **The clock** — `performance.now` is looked up on every read, never captured, so
 *   `vi.spyOn(performance, 'now').mockReturnValue(…)` moves "now" for both the deadline written by
 *   `setAuthenticated` and the check in `accessTokenForRequest`.
 * - **The 409 back-off** — plain `setTimeout`, so `vi.useFakeTimers()` +
 *   `vi.advanceTimersByTimeAsync(300)` / `(1000)` drives it. The delays are exported
 *   (`REFRESH_CONFLICT_RETRY_DELAYS_MS`) so a test names them instead of repeating the numbers.
 * - **`__resetForTests()`** — module state outlives a test (it is module scope; Vitest isolates
 *   *files*, not tests), so call it in `beforeEach`. It puts the store back to `booting`, forgets
 *   the boot promise and the in-flight refresh, and drops every listener. Work still pending from a
 *   previous test (a back-off timer, an unresolved fetch) belongs to an older **generation** and
 *   must not dispatch into the fresh store when it lands.
 */
import type { AuthState, SignOutReason } from './authMachine';
import type { AuthenticatedResponse, User } from './types';

/** A request whose token has less than this left refreshes **first** (AC-38). */
export const REFRESH_AHEAD_MS = 30_000;

/**
 * The waits before the second and third attempt when a refresh answers 409 `refresh_in_progress`
 * (another tab won the race inside the server's grace). After the last one the store goes
 * `unavailable` — never `anonymous`, because the user *is* logged in, just not provably yet (I-54).
 */
export const REFRESH_CONFLICT_RETRY_DELAYS_MS: readonly number[] = [300, 1000];

/**
 * What React may see: the state **without** the token. `useAuth()` is the only reader
 * (AC-35), and a token that is not in the snapshot cannot end up in a component, a React DevTools
 * panel or a test's rendered output.
 *
 * Referentially stable: `getSnapshot()` returns the same object until the underlying state
 * changes, as `useSyncExternalStore` requires (a fresh object every call is an infinite render
 * loop).
 */
export type AuthSnapshot =
  | { readonly status: 'booting' }
  | { readonly status: 'anonymous'; readonly reason: SignOutReason | null }
  | { readonly status: 'authenticated' }
  | { readonly status: 'unavailable' };

/**
 * How a refresh ended, for the caller that has to act on it. Never a rejection: a network error is
 * an outcome (`unavailable`), not an exception, so `bootstrap()` in `main.tsx` cannot produce an
 * unhandled rejection.
 *
 * `user` is the profile the token response carried, so the caller can
 * `queryClient.setQueryData(['auth', 'me'], user)` without a second round trip. The store does not
 * keep it.
 */
export type RefreshResult =
  | { readonly kind: 'authenticated'; readonly user: User }
  | { readonly kind: 'anonymous'; readonly reason: SignOutReason | null }
  | { readonly kind: 'unavailable' };

interface ModuleState {
  /** Bumped by `__resetForTests`; async work captures it and drops its result if it changed. */
  readonly generation: number;
  state: AuthState;
  bootPromise: Promise<RefreshResult> | null;
  inFlightRefresh: Promise<RefreshResult> | null;
  readonly listeners: Set<() => void>;
}

function freshModuleState(generation: number): ModuleState {
  return {
    generation,
    state: { status: 'booting' },
    bootPromise: null,
    inFlightRefresh: null,
    listeners: new Set(),
  };
}

let current: ModuleState = freshModuleState(0);

/**
 * The skeleton's body. The arguments are taken so the real parameters are *used* (the compiler and
 * the linter both refuse an unused one) — only their count reaches the message, never a value,
 * because one of them is a token response.
 */
function notImplemented(name: string, ...args: readonly unknown[]): never {
  throw new Error(
    `authStore.${name}(${String(args.length)} args): not implemented (T36 skeleton; T38 GREEN)`,
  );
}

/**
 * Start the boot refresh — **idempotent**: the first call sends `POST /api/auth/refresh`, every
 * later call returns that same promise. Called once from `main.tsx` at module scope, before
 * render, not from an effect (`<StrictMode>` runs effects twice — AC-36). Resolves to how the boot
 * ended, after the store has dispatched `BOOT_OK` / `BOOT_ANON` / `BOOT_FAILED`.
 */
function bootstrap(): Promise<RefreshResult> {
  return notImplemented('bootstrap');
}

/**
 * Refresh now — **single-flight**: while one refresh is out, every caller gets the same promise.
 *
 * | Answer                                   | While `booting` | Otherwise                                      |
 * |------------------------------------------|-----------------|------------------------------------------------|
 * | 200                                      | `BOOT_OK`       | `AUTHENTICATED`                                |
 * | 401 `not_signed_in`                      | `BOOT_ANON`     | `SIGNED_OUT` reason `expired`                  |
 * | 401 `refresh_token_reused`               | `BOOT_ANON`     | `SIGNED_OUT` reason `reused`                   |
 * | 409 `refresh_in_progress`, after retries | `BOOT_FAILED`   | `REFRESH_FAILED`                               |
 * | network error, 5xx                       | `BOOT_FAILED`   | `REFRESH_FAILED`                               |
 *
 * A 409 is retried after 300 ms, then after 1000 ms (`REFRESH_CONFLICT_RETRY_DELAYS_MS`), and only
 * the third 409 is a failure. "While `booting`" means **the refresh started in `booting`** — it is
 * decided when the request goes out, not when the answer lands. A boot 401 that lands after a login
 * must arrive as `BOOT_ANON` (which the reducer ignores in `authenticated`), never as `SIGNED_OUT`,
 * which it would honour. Never rejects.
 */
function refresh(): Promise<RefreshResult> {
  return notImplemented('refresh');
}

/**
 * The Retry button on the `unavailable` notice: dispatch `RETRY` (→ `booting`) and refresh. Only
 * meaningful from `unavailable`; elsewhere the reducer ignores `RETRY` and this is a plain
 * `refresh()`.
 */
function retry(): Promise<RefreshResult> {
  return notImplemented('retry');
}

/**
 * The token for an `auth: 'required'` request, called by `api/client.ts` on every such request.
 *
 * - `authenticated` with at least `REFRESH_AHEAD_MS` left → the token, no network.
 * - `authenticated` with less → `refresh()` first, then the new token (or `null` if it failed).
 * - `booting` → waits for the boot, then answers as above.
 * - `anonymous` / `unavailable` → `null`, no network: there is nothing to refresh with that the
 *   boot did not already try.
 */
function accessTokenForRequest(): Promise<string | null> {
  return notImplemented('accessTokenForRequest');
}

/**
 * A login or register succeeded: dispatch `AUTHENTICATED` with the response's token and a deadline
 * of `performance.now() + expires_in * 1000`. The mutation hook seeds `['auth', 'me']` itself.
 */
function setAuthenticated(response: AuthenticatedResponse): void {
  notImplemented('setAuthenticated', response);
}

/**
 * Dispatch `SIGNED_OUT` — the token is dropped with the `authenticated` variant that held it.
 * Local only: the caller has already called `api/auth.logout()` (and kept the user logged in if it
 * answered 503, I-31).
 */
function signOut(reason: SignOutReason): void {
  notImplemented('signOut', reason);
}

/** `useSyncExternalStore`'s subscribe: call `listener` after every state change; returns unsubscribe. */
function subscribe(listener: () => void): () => void {
  return notImplemented('subscribe', listener);
}

/** `useSyncExternalStore`'s getSnapshot: the token-free view, identity-stable between changes. */
function getSnapshot(): AuthSnapshot {
  return notImplemented('getSnapshot');
}

/**
 * The store. Methods are plain functions over module state, so they can be passed as callbacks
 * (`useSyncExternalStore(authStore.subscribe, authStore.getSnapshot)`) without binding.
 */
export const authStore = {
  bootstrap,
  refresh,
  retry,
  accessTokenForRequest,
  setAuthenticated,
  signOut,
  subscribe,
  getSnapshot,
} as const;

/**
 * **Test seam — never call from production code.** Back to a fresh `booting` store: no boot
 * promise, no in-flight refresh, no listeners, and a new generation so that anything still pending
 * from before the reset lands on nothing. See the module docstring.
 */
export function __resetForTests(): void {
  current = freshModuleState(current.generation + 1);
}
