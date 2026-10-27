import { useEffect, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router';

import { ApiError } from '@/api/client';
import { NotFoundPage } from '@/features/tailoring/components/NotFoundPage';

import {
  ADMIN_RETRY_LABEL,
  BACK_TO_USERS,
  USER_ERROR,
  USER_GONE,
  USER_LOADING,
  unlinkFailuresNote,
} from '../adminCopy';
import { contentNoticeFrom } from '../contentNotice';
import { useAdminUser } from '../hooks/useAdminUser';

import { AccountActions } from './AccountActions';
import { AccountContent } from './AccountContent';
import { AccountOverview } from './AccountOverview';

export interface UserPageProps {
  readonly actorId: string;
}

/**
 * `/admin/users/:userId` (AC-33). Two 404s mean different things: `user_not_found` is "this
 * account no longer exists", while the firewall's code-less 404 (a demotion) is the app's own
 * not-found page. Both outrank cached data; any other error does not, so a background refetch
 * failing never hides the overview.
 */
export function UserPage({ actorId }: UserPageProps): React.JSX.Element {
  // Always set under `users/:userId`; '' only satisfies the type.
  const userId = useParams().userId ?? '';
  const user = useAdminUser(actorId, userId);
  const error = user.error;
  const location = useLocation();
  const navigate = useNavigate();
  // A content delete's notice (slice 4.3, AC-29), read once; the history entry is then replaced
  // without it, so the sentence does not come back on back/forward — `UserListPage`'s pattern.
  const [notice] = useState(() => contentNoticeFrom(location.state));
  const carriesNotice = contentNoticeFrom(location.state) !== null;

  useEffect(() => {
    if (carriesNotice) {
      void navigate(
        { pathname: location.pathname, search: location.search, hash: location.hash },
        { replace: true, state: null },
      );
    }
  }, [carriesNotice, navigate, location.pathname, location.search, location.hash]);

  if (error instanceof ApiError && error.status === 404) {
    if (error.code !== 'user_not_found') return <NotFoundPage />;
    return (
      <section className="mb-10 space-y-3">
        <p className="text-slate-700">{USER_GONE}</p>
        <Link to="/admin" className="font-medium text-brand-700 underline underline-offset-2">
          {BACK_TO_USERS}
        </Link>
      </section>
    );
  }
  if (user.data !== undefined) {
    return (
      <section className="mb-10">
        {notice !== null && (
          <div
            role="status"
            className="mb-6 rounded-lg border border-slate-200 bg-surface-sunk p-4 text-sm text-slate-800"
          >
            <p>{notice.text}</p>
            {notice.unlinkFailures > 0 && <p>{unlinkFailuresNote(notice.unlinkFailures)}</p>}
          </div>
        )}
        <AccountOverview user={user.data} />
        <AccountActions actorId={actorId} user={user.data} />
        <AccountContent actorId={actorId} userId={userId} />
      </section>
    );
  }
  if (error !== null) {
    return (
      <div role="alert" className="mb-10 space-y-3">
        <p className="text-slate-700">{USER_ERROR}</p>
        <button
          type="button"
          onClick={() => {
            void user.refetch();
          }}
          disabled={user.isFetching}
          className="rounded-md bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60"
        >
          {ADMIN_RETRY_LABEL}
        </button>
      </div>
    );
  }
  return (
    <p role="status" className="mb-10 text-slate-600">
      {USER_LOADING}
    </p>
  );
}
