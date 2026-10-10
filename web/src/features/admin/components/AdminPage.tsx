import { ApiError } from '@/api/client';
import { NotFoundPage } from '@/features/tailoring/components/NotFoundPage';

import { ADMIN_CHECKING, ADMIN_RETRY_LABEL, ADMIN_UNAVAILABLE } from '../adminCopy';
import { useAdminAccess } from '../hooks/useAdminAccess';

import { AdminShell } from './AdminShell';

export interface AdminPageProps {
  /** The signed-in account, from `AccountScope`; roots the access probe's key (AC-34). */
  readonly userId: string;
}

/** The four things `/admin` can show (AC-31) — one union, so no two can render at once. */
export type AdminView = 'checking' | 'allowed' | 'not_found' | 'unavailable';

/**
 * **Error first.** A refetch on focus that answers 404 leaves the earlier 204 in `data`; checking
 * `data`/`isPending` before `error` would keep showing the shell to a demoted admin (R-26).
 */
function adminView(query: {
  readonly error: Error | null;
  readonly isPending: boolean;
}): AdminView {
  if (query.error !== null) {
    return query.error instanceof ApiError && query.error.status === 404
      ? 'not_found'
      : 'unavailable';
  }
  return query.isPending ? 'checking' : 'allowed';
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
      return <AdminShell />;
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
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-60"
          >
            {ADMIN_RETRY_LABEL}
          </button>
        </div>
      );
  }
}
