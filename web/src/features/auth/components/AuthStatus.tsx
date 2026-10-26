import { useId } from 'react';
import { NavLink, useMatch } from 'react-router';

import { BOARD_NAV_LABEL } from '@/features/tracking/trackingCopy';

import { AuthErrorNotice } from './AuthErrorNotice';
import {
  AUTH_BOOTING_NOTE,
  AUTH_RETRY_LABEL,
  AUTH_UNAVAILABLE_NOTE,
  HOW_IT_WORKS_NAV_LABEL,
  LOGOUT_LABEL,
  LOGOUT_PENDING_LABEL,
  WORKSPACE_NAV_LABEL,
} from '../authCopy';
import { useAuth } from '../hooks/useAuth';
import { useLogout } from '../hooks/useLogout';

const focusRing =
  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600';

/** A header link at every width: the current route's link is darker and underlined by a bar. */
const headerLinkClass = `border-b-2 border-transparent py-1.5 font-medium text-slate-600 hover:text-slate-900 aria-[current=page]:border-slate-900 aria-[current=page]:text-slate-900 ${focusRing}`;

/**
 * A signed-in nav item: a header link from `sm` up, an equal-width, 44 px-tall cell of the bottom
 * bar below it (AC-11). The current item is brand-coloured in the bar, where there is no underline.
 */
const navItemClass = `font-medium text-slate-600 hover:text-slate-900 aria-[current=page]:text-slate-900 sm:border-b-2 sm:border-transparent sm:py-1.5 sm:aria-[current=page]:border-slate-900 max-sm:flex max-sm:min-h-11 max-sm:flex-1 max-sm:items-center max-sm:justify-center max-sm:text-xs max-sm:font-semibold max-sm:aria-[current=page]:text-brand-700 ${focusRing}`;

/** A button that reads as a link (Retry, Log out). */
const buttonLinkClass = `font-medium text-brand-700 underline underline-offset-2 hover:text-slate-900 ${focusRing}`;

/**
 * The header's auth block (AC-42): four states, each distinguishable by role and text.
 *
 * - `booting` — a neutral, fixed-width placeholder, busy, with the words for assistive technology
 *   only. **No "Log in" flicker**: showing the anonymous links for the instant before the boot
 *   refresh answers tells a logged-in user they were logged out.
 * - `anonymous` — the empty state: **Log in** · **Create account** · **How it works**
 *   (web-design-refresh C-2).
 * - `authenticated` — **Workspace** (web-design-refresh C-1), **History** (slice 2.3, AC-44),
 *   **Board** (slice 3.1, AC-32), **Admin** for an admin (slice 4.1, AC-30), **Account**, the email
 *   and **Log out**. Below `sm` this same `<nav>` becomes the bottom bar (web-design-refresh AC-11,
 *   OQ-7: one element, so every link name occurs once); the email and Log out are hidden there and
 *   Log out is reached through Account (OQ-6). The
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
 * Every link is a `NavLink`, so the router — not a hand-written check — sets `aria-current="page"`
 * on the current route's link (web-design-refresh AC-9), and the active style keys off that attribute.
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
        <nav aria-label="Account" className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
          <NavLink to="/login" className={headerLinkClass}>
            Log in
          </NavLink>
          <NavLink to="/register" className={headerLinkClass}>
            Create account
          </NavLink>
          <NavLink to="/welcome" className={headerLinkClass}>
            {HOW_IT_WORKS_NAV_LABEL}
          </NavLink>
        </nav>
      );
    case 'authenticated':
      return (
        <nav
          aria-label="Account"
          className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm max-sm:fixed max-sm:inset-x-0 max-sm:bottom-0 max-sm:z-10 max-sm:flex-nowrap max-sm:gap-0 max-sm:border-t max-sm:border-slate-200 max-sm:bg-white max-sm:pb-[env(safe-area-inset-bottom)]"
        >
          {/* `end`: without it `/` is a prefix of every path and Workspace is always current. */}
          <NavLink to="/" end className={navItemClass}>
            {WORKSPACE_NAV_LABEL}
          </NavLink>
          <NavLink to="/history" className={navItemClass}>
            History
          </NavLink>
          <NavLink to="/board" className={navItemClass}>
            {BOARD_NAV_LABEL}
          </NavLink>
          {/* AC-30: a hint for showing the link, not authority — `/admin` asks the server (OQ-13). */}
          {auth.user?.role === 'admin' && (
            <NavLink to="/admin" className={navItemClass}>
              Admin
            </NavLink>
          )}
          <NavLink to="/account" className={navItemClass}>
            Account
          </NavLink>
          {auth.user !== undefined && (
            <span className="text-slate-600 max-sm:hidden">{auth.user.email}</span>
          )}
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
            className={buttonLinkClass}
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
    // Hidden below `sm`, error included: the bottom bar has no Log out; `/account` has its own.
    <div className="flex items-center gap-3 max-sm:hidden">
      <button
        type="button"
        onClick={() => {
          logout.mutate();
        }}
        disabled={logout.isPending}
        aria-describedby={logout.error === null ? undefined : errorId}
        className={`font-medium text-slate-600 hover:text-slate-900 disabled:opacity-60 ${focusRing}`}
      >
        {logout.isPending ? LOGOUT_PENDING_LABEL : LOGOUT_LABEL}
      </button>
      <AuthErrorNotice id={errorId} action="logout" error={logout.error} />
    </div>
  );
}
