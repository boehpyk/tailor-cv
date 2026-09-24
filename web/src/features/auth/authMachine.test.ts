import { describe, expect, it } from 'vitest';

import { INITIAL_AUTH_STATE, authReducer } from './authMachine';

import type { AuthEvent, AuthState } from './authMachine';

/**
 * T37 RED (AC-37) — `authReducer` walked over every (state, event) pair of technical plan §7's
 * amended table (2026-09-24), written against the table in `authMachine.ts`'s own docstring before
 * the reducer has a body (T36 skeleton throws). Four states × seven events = 28 pairs, every one
 * listed explicitly — no pair is left to a default branch nobody wrote a row for.
 *
 * `—` (ignored) pairs assert `toBe` the same `state` object, per the reducer's contract ("An ignored
 * event must return the same state object, so a subscriber comparing by identity sees no change").
 * Every other pair asserts the resulting value with `toEqual`, including the fields the event
 * carried (a fresh token, a `SignOutReason`) so a reducer that returns the right `status` but drops
 * the payload still goes red.
 */

const BOOTING: AuthState = { status: 'booting' };
const ANONYMOUS_NEVER: AuthState = { status: 'anonymous', reason: null };
const AUTHENTICATED_A: AuthState = {
  status: 'authenticated',
  accessToken: 'token-A',
  expiresAt: 1_000,
};
const UNAVAILABLE: AuthState = { status: 'unavailable' };

const BOOT_OK: AuthEvent = { type: 'BOOT_OK', accessToken: 'token-B', expiresAt: 2_000 };
const BOOT_ANON: AuthEvent = { type: 'BOOT_ANON' };
const BOOT_FAILED: AuthEvent = { type: 'BOOT_FAILED' };
const REFRESH_FAILED: AuthEvent = { type: 'REFRESH_FAILED' };
const AUTHENTICATED_EVENT: AuthEvent = {
  type: 'AUTHENTICATED',
  accessToken: 'token-C',
  expiresAt: 3_000,
};
const SIGNED_OUT_LOGGED_OUT: AuthEvent = { type: 'SIGNED_OUT', reason: 'logged_out' };
const SIGNED_OUT_EXPIRED: AuthEvent = { type: 'SIGNED_OUT', reason: 'expired' };
const SIGNED_OUT_REUSED: AuthEvent = { type: 'SIGNED_OUT', reason: 'reused' };
const RETRY: AuthEvent = { type: 'RETRY' };

type Case =
  | {
      readonly name: string;
      readonly state: AuthState;
      readonly event: AuthEvent;
      readonly kind: 'ignored';
    }
  | {
      readonly name: string;
      readonly state: AuthState;
      readonly event: AuthEvent;
      readonly kind: 'transition';
      readonly next: AuthState;
    };

// -------------------------------------------------------------------------------------------
// booting
// -------------------------------------------------------------------------------------------

const bootingCases: readonly Case[] = [
  {
    name: "booting + BOOT_OK moves to authenticated, carrying the event's token and deadline",
    state: BOOTING,
    event: BOOT_OK,
    kind: 'transition',
    next: { status: 'authenticated', accessToken: 'token-B', expiresAt: 2_000 },
  },
  {
    name: 'booting + BOOT_ANON moves to anonymous with no reason (nothing just ended)',
    state: BOOTING,
    event: BOOT_ANON,
    kind: 'transition',
    next: { status: 'anonymous', reason: null },
  },
  {
    name: 'booting + BOOT_FAILED moves to unavailable',
    state: BOOTING,
    event: BOOT_FAILED,
    kind: 'transition',
    next: UNAVAILABLE,
  },
  {
    name: 'booting + REFRESH_FAILED is ignored (that event is never dispatched while booting)',
    state: BOOTING,
    event: REFRESH_FAILED,
    kind: 'ignored',
  },
  {
    name: 'booting + AUTHENTICATED (a login racing the boot) moves straight to authenticated',
    state: BOOTING,
    event: AUTHENTICATED_EVENT,
    kind: 'transition',
    next: { status: 'authenticated', accessToken: 'token-C', expiresAt: 3_000 },
  },
  {
    name: "booting + SIGNED_OUT moves to anonymous, carrying the event's reason",
    state: BOOTING,
    event: SIGNED_OUT_LOGGED_OUT,
    kind: 'transition',
    next: { status: 'anonymous', reason: 'logged_out' },
  },
  {
    name: 'booting + RETRY is ignored (nothing to retry before the first answer)',
    state: BOOTING,
    event: RETRY,
    kind: 'ignored',
  },
];

// -------------------------------------------------------------------------------------------
// anonymous
// -------------------------------------------------------------------------------------------

const anonymousCases: readonly Case[] = [
  {
    name: 'anonymous + BOOT_OK is ignored (BOOT_* only means anything while booting)',
    state: ANONYMOUS_NEVER,
    event: BOOT_OK,
    kind: 'ignored',
  },
  {
    name: 'anonymous + BOOT_ANON is ignored',
    state: ANONYMOUS_NEVER,
    event: BOOT_ANON,
    kind: 'ignored',
  },
  {
    name: 'anonymous + BOOT_FAILED is ignored',
    state: ANONYMOUS_NEVER,
    event: BOOT_FAILED,
    kind: 'ignored',
  },
  {
    name: 'anonymous + REFRESH_FAILED is ignored (there is no refresh to fail while anonymous)',
    state: ANONYMOUS_NEVER,
    event: REFRESH_FAILED,
    kind: 'ignored',
  },
  {
    name: 'anonymous + AUTHENTICATED (login/register succeeded) moves to authenticated',
    state: ANONYMOUS_NEVER,
    event: AUTHENTICATED_EVENT,
    kind: 'transition',
    next: { status: 'authenticated', accessToken: 'token-C', expiresAt: 3_000 },
  },
  {
    name: 'anonymous + SIGNED_OUT stays anonymous but carries the new reason',
    state: ANONYMOUS_NEVER,
    event: SIGNED_OUT_EXPIRED,
    kind: 'transition',
    next: { status: 'anonymous', reason: 'expired' },
  },
  {
    name: 'anonymous + RETRY is ignored (Retry is only shown from unavailable)',
    state: ANONYMOUS_NEVER,
    event: RETRY,
    kind: 'ignored',
  },
];

// -------------------------------------------------------------------------------------------
// authenticated
// -------------------------------------------------------------------------------------------

const authenticatedCases: readonly Case[] = [
  {
    name: 'authenticated + BOOT_OK is ignored (a late boot answer must not overwrite a login)',
    state: AUTHENTICATED_A,
    event: BOOT_OK,
    kind: 'ignored',
  },
  {
    name: 'authenticated + BOOT_ANON is ignored — the race rule this table exists to enforce',
    state: AUTHENTICATED_A,
    event: BOOT_ANON,
    kind: 'ignored',
  },
  {
    name: 'authenticated + BOOT_FAILED is ignored',
    state: AUTHENTICATED_A,
    event: BOOT_FAILED,
    kind: 'ignored',
  },
  {
    name: 'authenticated + REFRESH_FAILED (amended 2026-09-24) moves to unavailable',
    state: AUTHENTICATED_A,
    event: REFRESH_FAILED,
    kind: 'transition',
    next: UNAVAILABLE,
  },
  {
    name: "authenticated + AUTHENTICATED replaces the token and deadline with the event's",
    state: AUTHENTICATED_A,
    event: AUTHENTICATED_EVENT,
    kind: 'transition',
    next: { status: 'authenticated', accessToken: 'token-C', expiresAt: 3_000 },
  },
  {
    name: 'authenticated + SIGNED_OUT (logout, or a refresh 401) moves to anonymous with the reason',
    state: AUTHENTICATED_A,
    event: SIGNED_OUT_REUSED,
    kind: 'transition',
    next: { status: 'anonymous', reason: 'reused' },
  },
  {
    name: 'authenticated + RETRY is ignored (Retry is only shown from unavailable)',
    state: AUTHENTICATED_A,
    event: RETRY,
    kind: 'ignored',
  },
];

// -------------------------------------------------------------------------------------------
// unavailable
// -------------------------------------------------------------------------------------------

const unavailableCases: readonly Case[] = [
  {
    name: 'unavailable + BOOT_OK is ignored',
    state: UNAVAILABLE,
    event: BOOT_OK,
    kind: 'ignored',
  },
  {
    name: 'unavailable + BOOT_ANON is ignored',
    state: UNAVAILABLE,
    event: BOOT_ANON,
    kind: 'ignored',
  },
  {
    name: 'unavailable + BOOT_FAILED is ignored',
    state: UNAVAILABLE,
    event: BOOT_FAILED,
    kind: 'ignored',
  },
  {
    name: 'unavailable + REFRESH_FAILED is ignored (RETRY is the only way out, not another failure)',
    state: UNAVAILABLE,
    event: REFRESH_FAILED,
    kind: 'ignored',
  },
  {
    name: 'unavailable + AUTHENTICATED (e.g. a login submitted while unavailable) moves to authenticated',
    state: UNAVAILABLE,
    event: AUTHENTICATED_EVENT,
    kind: 'transition',
    next: { status: 'authenticated', accessToken: 'token-C', expiresAt: 3_000 },
  },
  {
    name: 'unavailable + SIGNED_OUT moves to anonymous with the reason',
    state: UNAVAILABLE,
    event: SIGNED_OUT_LOGGED_OUT,
    kind: 'transition',
    next: { status: 'anonymous', reason: 'logged_out' },
  },
  {
    name: 'unavailable + RETRY moves to booting — the Retry button restarts the boot question',
    state: UNAVAILABLE,
    event: RETRY,
    kind: 'transition',
    next: BOOTING,
  },
];

const ALL_CASES: readonly Case[] = [
  ...bootingCases,
  ...anonymousCases,
  ...authenticatedCases,
  ...unavailableCases,
];

describe('authReducer: every (state, event) pair of the amended §7 table', () => {
  it('the hand-enumerated table is the full 4 states × 7 events = 28 pairs', () => {
    expect(ALL_CASES).toHaveLength(28);
  });

  it.each(ALL_CASES)('$name', (testCase) => {
    const result = authReducer(testCase.state, testCase.event);
    if (testCase.kind === 'ignored') {
      expect(result).toBe(testCase.state);
    } else {
      expect(result).toEqual(testCase.next);
    }
  });
});

describe('authReducer: purity', () => {
  it('does not mutate its state argument', () => {
    const before = { ...AUTHENTICATED_A };
    authReducer(AUTHENTICATED_A, SIGNED_OUT_LOGGED_OUT);
    expect(AUTHENTICATED_A).toEqual(before);
  });

  it('does not mutate its event argument', () => {
    const before = { ...BOOT_OK };
    authReducer(BOOTING, BOOT_OK);
    expect(BOOT_OK).toEqual(before);
  });
});

describe('INITIAL_AUTH_STATE', () => {
  it('is booting — where every page load starts', () => {
    expect(INITIAL_AUTH_STATE).toEqual({ status: 'booting' });
  });
});
