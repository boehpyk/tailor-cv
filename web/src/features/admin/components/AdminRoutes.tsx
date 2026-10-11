import { Route, Routes } from 'react-router';

import { NotFoundPage } from '@/features/tailoring/components/NotFoundPage';

import { UserListPage } from './UserListPage';
import { UserPage } from './UserPage';

export interface AdminRoutesProps {
  /** The signed-in admin, from `AccountScope`; roots every admin query key (AC-38). */
  readonly actorId: string;
}

/**
 * `/admin/*`'s descendant routes, **inside the lazy chunk** (AC-39): the list at the index, one
 * account at `users/:userId`, and the app's `NotFoundPage` for anything else under `/admin`.
 */
export function AdminRoutes({ actorId }: AdminRoutesProps): React.JSX.Element {
  return (
    <Routes>
      <Route index element={<UserListPage actorId={actorId} />} />
      <Route path="users/:userId" element={<UserPage actorId={actorId} />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
