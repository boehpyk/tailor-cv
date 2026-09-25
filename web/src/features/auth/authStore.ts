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
import { INITIAL_AUTH_STATE, authReducer } from './authMachine';

import { refresh as refreshRequest } from '@/api/auth';
import { ApiError } from '@/api/client';

import type { AuthEvent, AuthState, SignOutReason } from './authMachine';
import type { AuthenticatedResponse, User } from './types';

// **The import cycle, and why it is safe.** `api/client.ts` imports this module (for
// `accessTokenForRequest` and `refresh`), and this module imports `api/auth.ts`, which imports
// `api/client.ts`. ES modules resolve a cycle by handing out live bindings before the module body
// has run, so the rule is: **no module in the cycle may *use* an import at its top level.** Every
// use of `refreshRequest` and `ApiError` below is inside a function body that runs long after all
// three modules have finished evaluating. A top-level `ApiError.prototype…` or a store built by
// calling into the client at import time would read an uninitialised binding and throw.

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
  | { readonly kind: 'unavailable' }
  /**
   * The answer arrived and was **dropped**: something newer had already decided the state — a
   * login landed while the boot was out (the reducer ignores the boot answer), a sign-out of any
   * kind was honoured while a refresh was out (the store drops the answer: `signOuts`, see
   * `runRefresh`), or the store was reset. There is nothing for the caller to act on — in particular no `user` to seed, because
   * a boot refresh that loses to a login may carry a *different* user than the one now logged in.
   */
  | { readonly kind: 'superseded' };

interface ModuleState {
  /** Bumped by `__resetForTests`; async work captures it and drops its result if it changed. */
  readonly generation: number;
  /**
   * Bumped by every honoured `SIGNED_OUT`, whatever sent it — see `dispatch`. A refresh captures it
   * when its request goes out and drops its answer if it moved (the logout race; see `runRefresh`).
   */
  signOuts: number;
  state: AuthState;
  /** The token-free view of `state`, rebuilt only when `state` changes (identity-stable). */
  snapshot: AuthSnapshot;
  bootPromise: Promise<RefreshResult> | null;
  inFlightRefresh: Promise<RefreshResult> | null;
  readonly listeners: Set<() => void>;
}

function freshModuleState(generation: number): ModuleState {
  return {
    generation,
    signOuts: 0,
    state: INITIAL_AUTH_STATE,
    snapshot: snapshotOf(INITIAL_AUTH_STATE),
    bootPromise: null,
    inFlightRefresh: null,
    listeners: new Set(),
  };
}

let current: ModuleState = freshModuleState(0);

const SUPERSEDED: RefreshResult = { kind: 'superseded' };

/** The token-free view of a state. Built once per state change, never per read. */
function snapshotOf(state: AuthState): AuthSnapshot {
  switch (state.status) {
    case 'authenticated':
      // Deliberately *not* a spread of `state`: listing the one field keeps the token out by
      // construction rather than by remembering to delete it.
      return { status: 'authenticated' };
    case 'anonymous':
      return { status: 'anonymous', reason: state.reason };
    case 'booting':
    case 'unavailable':
      return { status: state.status };
  }
}

function sameSnapshot(a: AuthSnapshot, b: AuthSnapshot): boolean {
  if (a.status === 'anonymous' && b.status === 'anonymous') {
    return a.reason === b.reason;
  }
  return a.status === b.status;
}

/** Has `__resetForTests` run since `store` was captured? Stale work must land on nothing. */
function isCurrent(store: ModuleState): boolean {
  return store.generation === current.generation;
}

/**
 * Run `event` through the reducer and publish the result. Returns whether the event was
 * **honoured** — the reducer returns the very same object for an ignored event, so identity is
 * the answer, and no second copy of the table's "ignored" cells has to live here.
 */
function dispatch(store: ModuleState, event: AuthEvent): boolean {
  if (!isCurrent(store)) {
    return false;
  }
  const next = authReducer(store.state, event);
  if (next === store.state) {
    return false;
  }
  store.state = next;
  // Counted here, not in `signOut`, so that *every* path to `anonymous`-by-sign-out moves it —
  // `signOut`, `signOutIfHolding`, and a refresh's own 401 — without each having to remember.
  if (event.type === 'SIGNED_OUT') {
    store.signOuts += 1;
  }
  const nextSnapshot = snapshotOf(next);
  // A token rotation (authenticated → authenticated) changes nothing React can see; keeping the
  // old snapshot object means `useSyncExternalStore` does not re-render for it.
  if (!sameSnapshot(store.snapshot, nextSnapshot)) {
    store.snapshot = nextSnapshot;
  }
  // Copy first: a listener that unsubscribes while being notified must not skip its neighbour.
  for (const listener of [...store.listeners]) {
    listener();
  }
  return true;
}

function grantFrom(response: AuthenticatedResponse): { accessToken: string; expiresAt: number } {
  return {
    accessToken: response.access_token,
    expiresAt: performance.now() + response.expires_in * 1000,
  };
}

/** One `POST /api/auth/refresh`, classified by `code` — never by status alone. */
type AttemptOutcome =
  | { readonly kind: 'ok'; readonly response: AuthenticatedResponse }
  | { readonly kind: 'signed_out'; readonly reason: 'expired' | 'reused' }
  | { readonly kind: 'conflict' }
  | { readonly kind: 'failed' };

async function attemptRefresh(): Promise<AttemptOutcome> {
  try {
    return { kind: 'ok', response: await refreshRequest() };
  } catch (error) {
    // Not an `ApiError` means the request never got an answer (network down, a proxy's HTML page):
    // "could not find out", which is `failed` — not "you are logged out".
    if (!(error instanceof ApiError)) {
      return { kind: 'failed' };
    }
    switch (error.code) {
      case 'not_signed_in':
        return { kind: 'signed_out', reason: 'expired' };
      case 'refresh_token_reused':
        return { kind: 'signed_out', reason: 'reused' };
      case 'refresh_in_progress':
        return { kind: 'conflict' };
      default:
        // 5xx, 403 `origin_not_allowed`, an unknown code: we still do not know who this is.
        return { kind: 'failed' };
    }
  }
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/** Has the user been signed out since `signOutsAtStart` was read? Then this answer is stale. */
function signedOutSince(store: ModuleState, signOutsAtStart: number): boolean {
  return store.signOuts !== signOutsAtStart;
}

/** The first attempt plus one per entry in `REFRESH_CONFLICT_RETRY_DELAYS_MS`, 409s only. */
async function attemptWithConflictRetries(
  store: ModuleState,
  signOutsAtStart: number,
): Promise<AttemptOutcome> {
  for (let retry = 0; ; retry += 1) {
    const outcome = await attemptRefresh();
    if (outcome.kind !== 'conflict') {
      return outcome;
    }
    const delay = REFRESH_CONFLICT_RETRY_DELAYS_MS[retry];
    // Signed out meanwhile: the answer will be dropped anyway, so do not keep asking for it.
    if (delay === undefined || !isCurrent(store) || signedOutSince(store, signOutsAtStart)) {
      return { kind: 'failed' };
    }
    await wait(delay);
  }
}

/**
 * One refresh, start to finish. `startedBooting` is captured by the caller **before** the request
 * goes out: it is what decides `BOOT_*` versus the non-boot events, so a boot answer that lands
 * after a login still arrives as a boot answer — and the reducer ignores it.
 *
 * **A sign-out while the request is out supersedes it** (/verify round 1, finding 1). The reducer
 * cannot tell this case apart: it must honour `AUTHENTICATED` in `anonymous`, because a real login
 * submitted while logged out has to win (`authMachine.ts`'s table pins that). What it cannot know
 * is that *this* `AUTHENTICATED` answers a question asked before the logout — a refresh presenting
 * the cookie of a login the server has since deleted, or racing it. The store knows, so the store
 * decides: `signOuts` is read synchronously here, before the first `await`, and if any sign-out
 * has been honoured by the time the answer lands, every outcome is dropped — a 200 would undo the
 * logout, a 401 would overwrite its reason, a failure would mean nothing in `anonymous`.
 */
async function runRefresh(store: ModuleState, startedBooting: boolean): Promise<RefreshResult> {
  const signOutsAtStart = store.signOuts;
  const outcome = await attemptWithConflictRetries(store, signOutsAtStart);
  if (signedOutSince(store, signOutsAtStart)) {
    return SUPERSEDED;
  }

  switch (outcome.kind) {
    case 'ok': {
      const grant = grantFrom(outcome.response);
      const event: AuthEvent = startedBooting
        ? { type: 'BOOT_OK', ...grant }
        : { type: 'AUTHENTICATED', ...grant };
      return dispatch(store, event)
        ? { kind: 'authenticated', user: outcome.response.user }
        : SUPERSEDED;
    }
    case 'signed_out': {
      // A boot 401 is "there was no login to resume", not "your login just ended": no reason,
      // because nothing the user did or saw has ended.
      if (startedBooting) {
        return dispatch(store, { type: 'BOOT_ANON' })
          ? { kind: 'anonymous', reason: null }
          : SUPERSEDED;
      }
      return dispatch(store, { type: 'SIGNED_OUT', reason: outcome.reason })
        ? { kind: 'anonymous', reason: outcome.reason }
        : SUPERSEDED;
    }
    case 'conflict':
    case 'failed':
      return dispatch(store, { type: startedBooting ? 'BOOT_FAILED' : 'REFRESH_FAILED' })
        ? { kind: 'unavailable' }
        : SUPERSEDED;
  }
}

/**
 * Start the boot refresh — **idempotent**: the first call sends `POST /api/auth/refresh`, every
 * later call returns that same promise. Called once from `main.tsx` at module scope, before
 * render, not from an effect (`<StrictMode>` runs effects twice — AC-36). Resolves to how the boot
 * ended, after the store has dispatched `BOOT_OK` / `BOOT_ANON` / `BOOT_FAILED`.
 */
function bootstrap(): Promise<RefreshResult> {
  const store = current;
  store.bootPromise ??= refresh();
  return store.bootPromise;
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
 * which it would honour. When the reducer ignores the answer the result is `superseded`. Never
 * rejects.
 */
function refresh(): Promise<RefreshResult> {
  const store = current;
  if (store.inFlightRefresh !== null) {
    return store.inFlightRefresh;
  }
  const startedBooting = store.state.status === 'booting';
  const promise = runRefresh(store, startedBooting).then((result) => {
    // Clear only our own slot: after a reset, `store` is an orphaned object and nobody reads it.
    if (store.inFlightRefresh === promise) {
      store.inFlightRefresh = null;
    }
    return result;
  });
  store.inFlightRefresh = promise;
  return promise;
}

/**
 * The Retry button on the `unavailable` notice: dispatch `RETRY` (→ `booting`) and refresh. Only
 * meaningful from `unavailable`; elsewhere the reducer ignores `RETRY` and this is a plain
 * `refresh()`.
 */
function retry(): Promise<RefreshResult> {
  dispatch(current, { type: 'RETRY' });
  return refresh();
}

/** The token if the state holds one, else `null`. Synchronous; no refresh. */
function heldToken(store: ModuleState): string | null {
  return isCurrent(store) && store.state.status === 'authenticated'
    ? store.state.accessToken
    : null;
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
async function accessTokenForRequest(): Promise<string | null> {
  const store = current;
  if (store.state.status === 'booting') {
    // After a Retry the question is being asked again by a refresh that is not the boot promise.
    await (store.inFlightRefresh ?? bootstrap());
  }
  const state = store.state;
  if (!isCurrent(store) || state.status !== 'authenticated') {
    return null;
  }
  if (state.expiresAt - performance.now() >= REFRESH_AHEAD_MS) {
    return state.accessToken;
  }
  await refresh();
  return heldToken(store);
}

/**
 * A login or register succeeded: dispatch `AUTHENTICATED` with the response's token and a deadline
 * of `performance.now() + expires_in * 1000`. The mutation hook seeds `['auth', 'me']` itself.
 */
function setAuthenticated(response: AuthenticatedResponse): void {
  dispatch(current, { type: 'AUTHENTICATED', ...grantFrom(response) });
}

/**
 * Dispatch `SIGNED_OUT` — the token is dropped with the `authenticated` variant that held it.
 * Local only: the caller has already called `api/auth.logout()` (and kept the user logged in if it
 * answered 503, I-31).
 */
function signOut(reason: SignOutReason): void {
  dispatch(current, { type: 'SIGNED_OUT', reason });
}

/**
 * A bearer-authenticated request sent with `sentToken` answered 401 `not_signed_in`: the server
 * says the login behind that token is gone (the user was deleted). Dispatch `SIGNED_OUT` with
 * reason `expired` — **only if the store still holds `sentToken`**. If the token has changed since
 * the request went out (a rotation, or a logout and a new login), the answer describes a login
 * this tab has already moved past, and ending the current one on its word would be wrong. Called
 * by `api/client.ts`, which is where the server's codes are classified.
 */
function signOutIfHolding(sentToken: string): void {
  if (heldToken(current) === sentToken) {
    signOut('expired');
  }
}

/** `useSyncExternalStore`'s subscribe: call `listener` after every state change; returns unsubscribe. */
function subscribe(listener: () => void): () => void {
  const store = current;
  store.listeners.add(listener);
  return () => {
    store.listeners.delete(listener);
  };
}

/** `useSyncExternalStore`'s getSnapshot: the token-free view, identity-stable between changes. */
function getSnapshot(): AuthSnapshot {
  return current.snapshot;
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
  signOutIfHolding,
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
