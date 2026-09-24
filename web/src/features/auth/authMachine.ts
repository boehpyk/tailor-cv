/**
 * The auth state, as one pure reducer (AC-37; technical plan §7) — the pure half of
 * `authStore.ts`, in the shape 1.4's `autosaveMachine.ts` established.
 *
 * **Why a reducer and not four setters.** The store has several async sources that finish in any
 * order: the boot refresh, a login, a register, a proactive refresh, a logout. Written as
 * `state = 'authenticated'` in each callback, the order they happen to resolve in *is* the logic,
 * and the bug is always the same one — a boot refresh that started before a login and answered
 * after it overwrites the login with "anonymous". Here each source becomes an **event**, and one
 * function decides what an event means in the state it arrives in. Every (state, event) pair is a
 * row in a table a test can walk.
 *
 * **The table** (`—` = ignored: returns the same state object):
 *
 * | State \ Event   | BOOT_OK       | BOOT_ANON | BOOT_FAILED | REFRESH_FAILED | AUTHENTICATED  | SIGNED_OUT | RETRY   |
 * |-----------------|---------------|-----------|-------------|----------------|----------------|------------|---------|
 * | `booting`       | authenticated | anonymous | unavailable | —              | authenticated  | anonymous  | —       |
 * | `anonymous`     | —             | —         | —           | —              | authenticated  | anonymous  | —       |
 * | `authenticated` | —             | —         | —           | unavailable    | authenticated* | anonymous  | —       |
 * | `unavailable`   | —             | —         | —           | —              | authenticated  | anonymous  | booting |
 *
 * `*` the token and its deadline are replaced by the event's.
 *
 * The `BOOT_*` events are honoured **only** in `booting`: a boot result is an answer to "who was
 * this browser when the page loaded?", and once anything else has happened that question is stale.
 *
 * **`REFRESH_FAILED` is not `BOOT_FAILED`** (amended 2026-09-24, technical plan §7; AC-38 won over
 * the original table). A refresh made *after* the boot — proactive near expiry, or the
 * interceptor's one retry — that ends in a network error, a 5xx, or a 409 past its two retries
 * must take a logged-in user to `unavailable` ("couldn't check" + Retry), not leave them
 * `authenticated` holding a token that no longer works. Folding that into `BOOT_FAILED` would have
 * meant honouring `BOOT_FAILED` in `authenticated` too — and then a late boot failure could
 * overwrite a login that happened meanwhile, the exact bug the table exists to prevent. Two events,
 * two meanings, and each is honoured in exactly one state.
 *
 * **What is *not* in here.** No clock, no fetch, no timer. `expiresAt` arrives on the event,
 * already computed by the store from `performance.now()`; the reducer only carries it. That is what
 * makes the whole table decidable with plain objects.
 */

/** Why a session ended — carried for copy ("You've been logged out" vs "Your login expired"). */
export type SignOutReason =
  /** The user pressed Log out. */
  | 'logged_out'
  /** A refresh answered 401 `not_signed_in`: the login is gone (expired, revoked, user deleted). */
  | 'expired'
  /** A refresh answered 401 `refresh_token_reused`: the server revoked the login as a replay. */
  | 'reused';

/**
 * A usable access token and the moment it stops being usable, on the `performance.now()` clock
 * (milliseconds since the page's time origin — monotonic, and meaningless outside this page).
 */
export interface AccessGrant {
  readonly accessToken: string;
  readonly expiresAt: number;
}

export type AuthState =
  /** The boot refresh has not answered. Nothing is known — render neither "Log in" nor a user. */
  | { readonly status: 'booting' }
  /** No login. `reason` is why a session just ended, or `null` when there never was one. */
  | { readonly status: 'anonymous'; readonly reason: SignOutReason | null }
  /** Logged in. The token lives on this variant, so "authenticated with no token" has no type. */
  | ({ readonly status: 'authenticated' } & AccessGrant)
  /**
   * Could not find out (network, 5xx, or a `refresh_in_progress` that outlasted its retries) —
   * at boot (`BOOT_FAILED`) or on a later refresh (`REFRESH_FAILED`).
   * Distinct from `anonymous` on purpose: a user shown "Log in" when they are in fact logged in
   * logs in again and mints a second login (AC-42, I-51, I-54).
   */
  | { readonly status: 'unavailable' };

export type AuthEvent =
  /** The boot refresh answered 200. */
  | ({ readonly type: 'BOOT_OK' } & AccessGrant)
  /** The boot refresh answered 401 — there is no login to resume. */
  | { readonly type: 'BOOT_ANON' }
  /** The boot refresh failed: network error, 5xx, or 409s past the retry budget. */
  | { readonly type: 'BOOT_FAILED' }
  /**
   * A refresh made while **not** booting failed the same ways. Moves `authenticated` to
   * `unavailable`; ignored everywhere else.
   */
  | { readonly type: 'REFRESH_FAILED' }
  /** A login, a register or a non-boot refresh succeeded. */
  | ({ readonly type: 'AUTHENTICATED' } & AccessGrant)
  /** Logout succeeded, or a refresh answered 401. */
  | { readonly type: 'SIGNED_OUT'; readonly reason: SignOutReason }
  /** The user pressed Retry on the `unavailable` notice. */
  | { readonly type: 'RETRY' };

/** Where every page load starts. */
export const INITIAL_AUTH_STATE: AuthState = { status: 'booting' };

/**
 * `(state, event) → state`. Pure: no I/O, no clock, no mutation of its arguments. An ignored event
 * returns the **same** `state` object, so a subscriber comparing by identity sees no change.
 */
export function authReducer(state: AuthState, event: AuthEvent): AuthState {
  switch (event.type) {
    // A boot answer means something only while the boot question is still open.
    case 'BOOT_OK':
      return state.status === 'booting' ? grantedState(event) : state;
    case 'BOOT_ANON':
      return state.status === 'booting' ? { status: 'anonymous', reason: null } : state;
    case 'BOOT_FAILED':
      return state.status === 'booting' ? { status: 'unavailable' } : state;

    // Only a logged-in user has a refresh that can fail *after* the boot.
    case 'REFRESH_FAILED':
      return state.status === 'authenticated' ? { status: 'unavailable' } : state;

    // A login, register or later refresh is fresh news in every state.
    case 'AUTHENTICATED':
      return grantedState(event);
    case 'SIGNED_OUT':
      return { status: 'anonymous', reason: event.reason };

    // Retry is the way out of `unavailable`, and only of it.
    case 'RETRY':
      return state.status === 'unavailable' ? { status: 'booting' } : state;
  }
}

/**
 * Copy only the grant's two fields: an event object also carries `type`, and spreading it into
 * the state would smuggle that in (and make the state share structure with the event).
 */
function grantedState(grant: AccessGrant): AuthState {
  return { status: 'authenticated', accessToken: grant.accessToken, expiresAt: grant.expiresAt };
}
