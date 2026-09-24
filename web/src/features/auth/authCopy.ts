/**
 * The words the auth screens say, kept out of the components that say them — `exportCopy.ts`'s
 * pattern (technical plan §7, AC-39, AC-40).
 *
 * A string a test pins and a string a component happens to contain are different things, and the
 * difference shows up the day someone reflows the JSX. Every sentence the spec states verbatim
 * lives here, in a module whose whole content is that copy.
 *
 * **Every refusal is chosen by `code`, never by `message`.** `message` is the server's prose and
 * can be reworded without notice; `code` is the contract (`api/client.ts`). The switch below is
 * exhaustive over `AuthErrorCode` — the closed set `/api/auth/*` can answer with — so a new code in
 * `types.ts` is a TypeScript error here, at the one place that must write its sentence, rather than
 * a blank alert in somebody's browser. Two shapes sit outside that set and each gets its own
 * sentence: no answer at all (a network failure), and a code this client does not know (the wire's
 * `code` is an open set, so there is a fallback).
 */

import { ApiError } from '@/api/client';

import { isAuthErrorCode } from './types';

import type { AuthErrorCode } from './types';

/** Which form a refusal answered — the same code reads differently on each ("Logging in is…"). */
export type AuthAction = 'login' | 'register' | 'logout';

/**
 * One notice's content. `link` is set where the notice offers a way forward in words the user can
 * click — `email_already_registered` → **Log in** (AC-40).
 */
export interface AuthErrorCopy {
  readonly message: string;
  readonly link?: { readonly to: string; readonly label: string };
}

// --- Static copy ----------------------------------------------------------------------------------

/** The submit buttons, idle and pending. The pending label *is* the "still working" state. */
export const LOGIN_SUBMIT_LABEL = 'Log in';
export const LOGIN_PENDING_LABEL = 'Logging in…';
export const REGISTER_SUBMIT_LABEL = 'Create account';
export const REGISTER_PENDING_LABEL = 'Creating your account…';
export const LOGOUT_LABEL = 'Log out';
export const LOGOUT_PENDING_LABEL = 'Logging out…';

/**
 * The password hint under `/register`'s field.
 *
 * **Copy, not a rule** (Constitution §4.5). The number here describes today's policy to a person
 * about to type; it is never compared against anything. The API is the authority: it refuses a
 * short password with `password_too_short` and its own `min_length`, and the refusal's sentence is
 * built from *that* number (see `passwordTooShort` below). If the server's policy changes and this
 * line does not, the user reads a stale hint and a correct refusal — never a wrong refusal.
 */
export const PASSWORD_HINT = 'At least 12 characters. A few unrelated words make a good one.';

/** AC-40's two static notices on `/register`, verbatim. Pinned by a test; edit on purpose. */
export const REGISTER_STORAGE_NOTICE =
  'We store your email address and a one-way hash of your password — never the password itself.';
export const REGISTER_GUEST_WORK_NOTICE =
  'Work you did as a guest stays with this browser for 24 hours and is not moved into your account yet.';

/** AC-42 / I-51 — the header and the guard both say this when the boot refresh could not answer. */
export const AUTH_UNAVAILABLE_NOTE = "Couldn't check whether you're logged in";
export const AUTH_RETRY_LABEL = 'Retry';

/** AC-42's `booting` state — visually hidden, read by assistive technology. */
export const AUTH_BOOTING_NOTE = 'Checking your login…';

/** `/account` (AC-41). */
export const ACCOUNT_LOADING_NOTE = 'Loading your account…';
export const ACCOUNT_ERROR_NOTE = "Couldn't load your account";
/**
 * Not an empty state, stated: an account in 2.1 owns nothing that could be empty. This line says
 * what is coming so the page does not read as broken.
 */
export const ACCOUNT_NEXT_RELEASE_NOTE =
  'Saving your CVs and tailored documents to your account arrives in the next release.';

// --- Refusals -------------------------------------------------------------------------------------

/** A thrown value that is not an `ApiError` never reached a response: offline, DNS, a dead proxy. */
const NETWORK_FAILURE = "Couldn't reach TailorCraft.";

/**
 * The one sentence for every logout failure (I-31). A failed logout **leaves the user logged in** —
 * the server did not end the login and did not clear the cookie — so whatever the reason, the true
 * thing to say is that it did not happen and can be tried again.
 */
const LOGOUT_FAILED = "Couldn't log you out. Try again.";

/**
 * The notice for `error` on `action`'s form. Takes the whole error, not just its `code`, because
 * two sentences carry a number the server sent: `rate_limited` (`retryAfterSeconds`) and
 * `password_too_short` / `password_too_long` (`details.min_length` / `details.max_length`).
 */
export function authErrorCopy(action: AuthAction, error: Error): AuthErrorCopy {
  if (action === 'logout') {
    return { message: LOGOUT_FAILED };
  }
  if (!(error instanceof ApiError)) {
    return { message: NETWORK_FAILURE };
  }
  if (!isAuthErrorCode(error.code)) {
    return {
      message:
        error.status >= 500
          ? 'Something went wrong on our side. Try again.'
          : 'Something went wrong. Try again.',
    };
  }
  return copyForCode(error.code, action, error);
}

function copyForCode(
  code: AuthErrorCode,
  action: Exclude<AuthAction, 'logout'>,
  error: ApiError,
): AuthErrorCopy {
  switch (code) {
    case 'invalid_credentials':
      // One sentence for "no such email" and "wrong password" — the server made them byte-identical
      // on purpose (AC-28), and the copy must not undo that by guessing which it was.
      return { message: "That email and password don't match an account." };
    case 'invalid_email':
      return { message: "That doesn't look like an email address. Check it and try again." };
    case 'password_too_short':
      return { message: passwordTooShort(error) };
    case 'password_too_long':
      return { message: passwordTooLong(error) };
    case 'password_matches_email':
      return { message: "Your password can't be your email address. Choose something else." };
    case 'email_already_registered':
      return {
        message: 'An account with this email already exists.',
        link: { to: '/login', label: 'Log in' },
      };
    case 'rate_limited':
      return { message: tooManyAttempts(error.retryAfterSeconds) };
    case 'rate_limit_unavailable':
    case 'service_unavailable':
      // Two causes (Redis down, Postgres down), one meaning for the user: "not right now". And the
      // second clause is the one that matters to someone mid-task — their guest work is fine.
      return {
        message: `${action === 'login' ? 'Logging in' : 'Creating an account'} is unavailable right now. Anything you're doing as a guest is unaffected.`,
      };
    case 'validation_error':
      return { message: 'Enter your email address and a password, then try again.' };
    case 'origin_not_allowed':
      return { message: 'This page is out of date. Reload TailorCraft and try again.' };
    case 'not_signed_in':
    case 'refresh_token_reused':
    case 'refresh_in_progress':
    case 'invalid_access_token':
      // Answers from refresh / me, never from these two forms. Written down so the switch stays
      // exhaustive; if one ever arrives here, "try again" is still true.
      return { message: 'Something went wrong. Try again.' };
  }
}

/** `Retry-After: 120` → "…in 2 minutes." Rounded **up**: telling someone "0 minutes" invites a 429. */
function tooManyAttempts(retryAfterSeconds: number | null): string {
  if (retryAfterSeconds === null) {
    return 'Too many attempts. Try again in a few minutes.';
  }
  const minutes = Math.max(1, Math.ceil(retryAfterSeconds / 60));
  return `Too many attempts. Try again in ${String(minutes)} ${minutes === 1 ? 'minute' : 'minutes'}.`;
}

/** The server's own number, narrowed here — the one place that knows what the key means. */
function numberDetail(error: ApiError, key: string): number | null {
  const value = error.details[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function passwordTooShort(error: ApiError): string {
  const min = numberDetail(error, 'min_length');
  return min === null ? 'That password is too short.' : `Use at least ${String(min)} characters.`;
}

function passwordTooLong(error: ApiError): string {
  const max = numberDetail(error, 'max_length');
  return max === null ? 'That password is too long.' : `Use at most ${String(max)} characters.`;
}
