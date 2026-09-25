import {
  ACCOUNT_ERROR_NOTE,
  ACCOUNT_LOADING_NOTE,
  ACCOUNT_NEXT_RELEASE_NOTE,
  AUTH_RETRY_LABEL,
  LOGOUT_LABEL,
  LOGOUT_PENDING_LABEL,
} from '../authCopy';
import { useCurrentUser } from '../hooks/useCurrentUser';
import { useLogout } from '../hooks/useLogout';

import { AuthErrorNotice } from './AuthErrorNotice';

/**
 * "5 January 2026". The instant is whole-second UTC from the server; formatting it in UTC keeps
 * a user just east or west of midnight from reading a different day than the one they joined on.
 */
const memberSinceFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'long',
  timeZone: 'UTC',
});

/**
 * `/account` (AC-41), rendered inside `RequireAuth` — which is why it does not check auth itself.
 *
 * Two independent requests, two independent sets of states:
 *
 * - **The profile** is `useCurrentUser`'s query: loading, error + **Retry** (`refetch`), success.
 *   There is no empty state, and that is stated rather than implied: an account in 2.1 owns nothing
 *   that could be empty (`ACCOUNT_NEXT_RELEASE_NOTE` says what is coming).
 * - **Log out** is `useLogout`: pending ("Logging out…", disabled) and error. On error the user
 *   **stays logged in** (I-31) — the hook does nothing locally, and this page keeps showing the
 *   account, because that is what is true.
 */
export function AccountPage() {
  const currentUser = useCurrentUser();
  const logout = useLogout();

  return (
    <section aria-labelledby="account-heading" className="mb-10">
      <h2 id="account-heading" className="mb-4 text-lg font-semibold text-slate-900">
        Your account
      </h2>
      <AccountBody query={currentUser} />

      <div className="mt-6 space-y-3">
        <AuthErrorNotice id="logout-error" action="logout" error={logout.error} />
        <button
          type="button"
          onClick={() => {
            logout.mutate();
          }}
          disabled={logout.isPending}
          aria-describedby={logout.error === null ? undefined : 'logout-error'}
          className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-900 disabled:opacity-60"
        >
          {logout.isPending ? LOGOUT_PENDING_LABEL : LOGOUT_LABEL}
        </button>
      </div>
    </section>
  );
}

function AccountBody({ query }: { readonly query: ReturnType<typeof useCurrentUser> }) {
  if (query.isPending) {
    return (
      <p role="status" className="text-slate-600">
        {ACCOUNT_LOADING_NOTE}
      </p>
    );
  }
  if (query.isError) {
    return (
      <div role="alert" className="space-y-3">
        <p className="text-slate-700">{ACCOUNT_ERROR_NOTE}</p>
        <button
          type="button"
          onClick={() => {
            void query.refetch();
          }}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white"
        >
          {AUTH_RETRY_LABEL}
        </button>
      </div>
    );
  }
  const user = query.data;
  return (
    <div className="space-y-2">
      <dl className="space-y-1">
        <dt className="sr-only">Email</dt>
        <dd className="font-medium text-slate-900">{user.email}</dd>
        <dt className="sr-only">Joined</dt>
        <dd className="text-sm text-slate-600">
          Member since {memberSinceFormat.format(new Date(user.created_at))}
        </dd>
      </dl>
      <p className="text-sm text-slate-500">{ACCOUNT_NEXT_RELEASE_NOTE}</p>
    </div>
  );
}
