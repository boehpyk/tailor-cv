import { useRoutes } from 'react-router';

import { NotFoundPage } from '@/features/tailoring/components/NotFoundPage';

import { AdminCvPage } from './AdminCvPage';
import { AdminPostingPage } from './AdminPostingPage';
import { AdminRunPage } from './AdminRunPage';
import { UserListPage } from './UserListPage';
import { UserPage } from './UserPage';

export interface AdminRoutesProps {
  /** The signed-in admin, from `AccountScope`; roots every admin query key (AC-38). */
  readonly actorId: string;
}

/**
 * `/admin/*`'s descendant routes, **inside the lazy chunk** (AC-39): the list at the index, one
 * account at `users/:userId`, its content's three read pages under it (slice 4.3, AC-32), and
 * the app's `NotFoundPage` for anything else under `/admin`.
 *
 * `useRoutes` with route objects, not `<Routes>` JSX: react-router lives in the main chunk, and
 * `<Routes>` pulls its `createRoutesFromChildren` in with it — measured +0.69 kB gzip on the main
 * chunk (AC-40's budget is 0.5) for the same three routes.
 */
export function AdminRoutes({ actorId }: AdminRoutesProps): React.JSX.Element | null {
  return useRoutes([
    { index: true, element: <UserListPage actorId={actorId} /> },
    { path: 'users/:userId', element: <UserPage actorId={actorId} /> },
    { path: 'users/:userId/cvs/:baseCvId', element: <AdminCvPage actorId={actorId} /> },
    { path: 'users/:userId/runs/:runId', element: <AdminRunPage actorId={actorId} /> },
    { path: 'users/:userId/postings/:postingId', element: <AdminPostingPage actorId={actorId} /> },
    { path: '*', element: <NotFoundPage /> },
  ]);
}
