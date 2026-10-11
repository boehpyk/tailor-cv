import { Link, useParams } from 'react-router';

import { ApiError } from '@/api/client';
import { NotFoundPage } from '@/features/tailoring/components/NotFoundPage';

import {
  ADMIN_RETRY_LABEL,
  BACK_TO_USERS,
  USER_ERROR,
  USER_GONE,
  USER_LOADING,
} from '../adminCopy';
import { useAdminUser } from '../hooks/useAdminUser';

import { AccountActions } from './AccountActions';
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

  if (error instanceof ApiError && error.status === 404) {
    if (error.code !== 'user_not_found') return <NotFoundPage />;
    return (
      <section className="mb-10 space-y-3">
        <p className="text-slate-700">{USER_GONE}</p>
        <Link to="/admin" className="font-medium text-slate-900 underline underline-offset-2">
          {BACK_TO_USERS}
        </Link>
      </section>
    );
  }
  if (user.data !== undefined) {
    return (
      <section className="mb-10">
        <AccountOverview user={user.data} />
        <AccountActions actorId={actorId} user={user.data} />
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
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-60"
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
