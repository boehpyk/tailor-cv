import { ApiError } from '@/api/client';
import { NotFoundPage } from '@/features/tailoring/components/NotFoundPage';

import { ADMIN_CHECKING, ADMIN_RETRY_LABEL, ADMIN_UNAVAILABLE } from '../adminCopy';
import { useAdminAccess } from '../hooks/useAdminAccess';

import { AdminRoutes } from './AdminRoutes';

export interface AdminPageProps {
  /** The signed-in account, from `AccountScope`; roots the access probe's key (AC-34). */
  readonly userId: string;
}

/** The four things `/admin` can show (AC-31) — one union, so no two can render at once. */
export type AdminView = 'checking' | 'allowed' | 'not_found' | 'unavailable';

/**
 * **Only a 404 outranks data** (slice 4.2, OQ-5; 4.1's carried MINOR). A refetch on focus that
 * answers 404 leaves the earlier 204 in `data`, and a demoted admin must still lose the page
 * (R-26) — so the 404 is checked first. A 503 or a network failure on that refetch says nothing
 * about the role, so a cached 204 keeps the page; only with no answer yet does an error show
 * *Couldn't check admin access.*
 */
function adminView(query: {
  readonly error: Error | null;
  readonly data: true | undefined;
}): AdminView {
  if (query.error instanceof ApiError && query.error.status === 404) return 'not_found';
  if (query.data !== undefined) return 'allowed';
  return query.error === null ? 'checking' : 'unavailable';
}

/**
 * The lazy target (default export, for `React.lazy`): reads `useAdminAccess` and renders the
 * `AdminView` (AC-31). The not-found view is the app's own `NotFoundPage`, so a non-admin cannot
 * tell `/admin` from a path that does not exist (OQ-2). No view moves focus (AC-36).
 */
export default function AdminPage({ userId }: AdminPageProps): React.JSX.Element {
  const access = useAdminAccess(userId);

  switch (adminView(access)) {
    case 'checking':
      return (
        <p role="status" className="mb-10 text-slate-600">
          {ADMIN_CHECKING}
        </p>
      );
    case 'allowed':
      return <AdminRoutes actorId={userId} />;
    case 'not_found':
      return <NotFoundPage />;
    case 'unavailable':
      return (
        <div role="alert" className="mb-10 space-y-3">
          <p className="text-slate-700">{ADMIN_UNAVAILABLE}</p>
          <button
            type="button"
            onClick={() => {
              void access.refetch();
            }}
            disabled={access.isFetching}
            className="rounded-md bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60"
          >
            {ADMIN_RETRY_LABEL}
          </button>
        </div>
      );
  }
}
