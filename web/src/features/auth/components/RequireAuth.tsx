import { Navigate, useLocation } from 'react-router';

import { ACCOUNT_DELETED_NOTICE } from '@/features/savedCvs/savedCvsCopy';

import {
  ACCOUNT_DISABLED_NOTICE,
  AUTH_BOOTING_NOTE,
  AUTH_RETRY_LABEL,
  AUTH_UNAVAILABLE_NOTE,
} from '../authCopy';
import { homeNoticeState } from '../homeNotice';
import { useAuth } from '../hooks/useAuth';

import type { SignOutReason } from '../authMachine';
import type { ReactNode } from 'react';

/**
 * The reasons with no account to log back in to: each goes to `/` with its notice instead of
 * `/login?next=…` (2.2's `account_deleted`; 4.2's `account_disabled`, AC-36).
 */
const HOME_NOTICE_FOR: Partial<Record<SignOutReason, string>> = {
  account_deleted: ACCOUNT_DELETED_NOTICE,
  account_disabled: ACCOUNT_DISABLED_NOTICE,
};

export interface RequireAuthProps {
  /** What to render once `useAuth()` says `authenticated`. */
  readonly children: ReactNode;
}

/**
 * The route guard (AC-41). Four states, four different answers:
 *
 * - `booting` → a loading state. **Not** a redirect: the boot refresh has not answered, and sending
 *   a logged-in user to `/login` for the 50 ms it takes would be a lie they can see.
 * - `anonymous` → `/login?next=<this path>`, so logging in brings them back — **except** when the
 *   reason is `account_deleted` (slice 2.2, AC-40): there is no account left to log in to, so the
 *   answer is `/` with "Your account and saved CVs were deleted." (and, from 4.2, reason
 *   `account_disabled` → `/` with its notice: a disabled account cannot log in either). This is the path that decides
 *   where a deletion lands; see `useDeleteAccount` for the race it closes.
 * - `unavailable` → an error with **Retry**, never a redirect. "We could not ask" is not "you are
 *   logged out"; a user shown the login form would log in again and mint a second login (AC-42).
 * - `authenticated` → `children`.
 */
export function RequireAuth({ children }: RequireAuthProps) {
  const auth = useAuth();
  const location = useLocation();

  switch (auth.status) {
    case 'booting':
      return (
        <p role="status" className="mb-10 text-slate-600">
          {AUTH_BOOTING_NOTE}
        </p>
      );
    case 'anonymous': {
      const notice = auth.reason === null ? undefined : HOME_NOTICE_FOR[auth.reason];
      if (notice !== undefined) {
        return <Navigate to="/" replace state={homeNoticeState(notice)} />;
      }
      const next = `${location.pathname}${location.search}`;
      return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
    }
    case 'unavailable':
      return (
        <div role="alert" className="mb-10 space-y-3">
          <p className="text-slate-700">{AUTH_UNAVAILABLE_NOTE}</p>
          <button
            type="button"
            onClick={auth.retry}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white"
          >
            {AUTH_RETRY_LABEL}
          </button>
        </div>
      );
    case 'authenticated':
      return <>{children}</>;
  }
}
