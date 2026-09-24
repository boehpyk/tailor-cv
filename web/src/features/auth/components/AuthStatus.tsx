import { Link } from 'react-router';

import { AUTH_BOOTING_NOTE, AUTH_RETRY_LABEL, AUTH_UNAVAILABLE_NOTE } from '../authCopy';
import { useAuth } from '../hooks/useAuth';

const linkClass = 'font-medium text-slate-900 underline underline-offset-2';

/**
 * The header's auth block (AC-42): four states, each distinguishable by role and text.
 *
 * - `booting` — a neutral, fixed-width placeholder, busy, with the words for assistive technology
 *   only. **No "Log in" flicker**: showing the anonymous links for the instant before the boot
 *   refresh answers tells a logged-in user they were logged out.
 * - `anonymous` — the empty state: **Log in** · **Create account**.
 * - `authenticated` — the email and **Account**. The email comes from `['auth', 'me']` and can be
 *   absent for the instant before the seed lands; the link cannot, so it renders alone.
 * - `unavailable` — "Couldn't check whether you're logged in" + **Retry**. Never the anonymous
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
        <nav aria-label="Account" className="flex items-center gap-4 text-sm">
          {auth.user !== undefined && <span className="text-slate-600">{auth.user.email}</span>}
          <Link to="/account" className={linkClass}>
            Account
          </Link>
        </nav>
      );
    case 'unavailable':
      return (
        <div className="flex items-center gap-3 text-sm">
          <span className="text-slate-700">{AUTH_UNAVAILABLE_NOTE}</span>
          <button type="button" onClick={auth.retry} className={linkClass}>
            {AUTH_RETRY_LABEL}
          </button>
        </div>
      );
  }
}
