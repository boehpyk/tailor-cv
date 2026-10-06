import { useId } from 'react';
import { Link, useMatch } from 'react-router';

import { BOARD_NAV_LABEL } from '@/features/tracking/trackingCopy';

import { AuthErrorNotice } from './AuthErrorNotice';
import {
  AUTH_BOOTING_NOTE,
  AUTH_RETRY_LABEL,
  AUTH_UNAVAILABLE_NOTE,
  LOGOUT_LABEL,
  LOGOUT_PENDING_LABEL,
} from '../authCopy';
import { useAuth } from '../hooks/useAuth';
import { useLogout } from '../hooks/useLogout';

const linkClass = 'font-medium text-slate-900 underline underline-offset-2';

/**
 * The header's auth block (AC-42): four states, each distinguishable by role and text.
 *
 * - `booting` — a neutral, fixed-width placeholder, busy, with the words for assistive technology
 *   only. **No "Log in" flicker**: showing the anonymous links for the instant before the boot
 *   refresh answers tells a logged-in user they were logged out.
 * - `anonymous` — the empty state: **Log in** · **Create account**.
 * - `authenticated` — the email, **History** (slice 2.3, AC-44), **Board** (slice 3.1, AC-32),
 *   **Account** and **Log out**. The
 *   email comes from `['auth', 'me']` and can be absent for the instant before the seed lands; the
 *   links cannot, so they render alone. Log out is here so it is one click from every page — a
 *   history run included (AC-43) — except `/account`, whose own Log out already sits in the page:
 *   two identically named buttons on one screen is one too many.
 * - `unavailable` — "Couldn't check whether you're logged in" + **Retry**, whose accessible name
 *   says what it retries: a page may offer its own Retry for the same state (`/`'s gate, AC-38),
 *   and two controls both announced as just "Retry" leave a screen-reader user guessing. Never the anonymous
 *   links, for the reason `RequireAuth` gives: a user who believes they were logged out logs in
 *   again and mints a second login.
 *
 * No props: everything comes from `useAuth()`.
 */
export function AuthStatus() {
  const auth = useAuth();

  switch (auth.status) {
    case 'booting':
      return (
        <div aria-busy="true" className="h-5 w-48">
          <span className="sr-only">{AUTH_BOOTING_NOTE}</span>
        </div>
      );
    case 'anonymous':
      return (
        <nav aria-label="Account" className="flex gap-4 text-sm">
          <Link to="/login" className={linkClass}>
            Log in
          </Link>
          <Link to="/register" className={linkClass}>
            Create account
          </Link>
        </nav>
      );
    case 'authenticated':
      return (
        <nav aria-label="Account" className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          {auth.user !== undefined && <span className="text-slate-600">{auth.user.email}</span>}
          <Link to="/history" className={linkClass}>
            History
          </Link>
          <Link to="/board" className={linkClass}>
            {BOARD_NAV_LABEL}
          </Link>
          <Link to="/account" className={linkClass}>
            Account
          </Link>
          <HeaderLogout />
        </nav>
      );
    case 'unavailable':
      return (
        <div className="flex items-center gap-3 text-sm">
          <span className="text-slate-700">{AUTH_UNAVAILABLE_NOTE}</span>
          <button
            type="button"
            onClick={auth.retry}
            aria-label={`${AUTH_RETRY_LABEL} checking your login`}
            className={linkClass}
          >
            {AUTH_RETRY_LABEL}
          </button>
        </div>
      );
  }
}

/**
 * The header's Log out — `useLogout`, the same mutation `/account` uses, so a logout from here
 * clears `['auth']` (every account query, AC-43), signs the store out and tells the other tabs.
 * Absent on `/account`, which has its own.
 */
function HeaderLogout(): React.JSX.Element | null {
  const onAccountPage = useMatch('/account') !== null;
  const logout = useLogout();
  const errorId = useId();

  if (onAccountPage) {
    return null;
  }
  return (
    <>
      <button
        type="button"
        onClick={() => {
          logout.mutate();
        }}
        disabled={logout.isPending}
        aria-describedby={logout.error === null ? undefined : errorId}
        className={`${linkClass} disabled:opacity-60`}
      >
        {logout.isPending ? LOGOUT_PENDING_LABEL : LOGOUT_LABEL}
      </button>
      <AuthErrorNotice id={errorId} action="logout" error={logout.error} />
    </>
  );
}
