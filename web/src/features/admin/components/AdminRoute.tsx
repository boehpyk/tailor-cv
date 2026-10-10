import { Suspense, lazy } from 'react';

import { RequireAuth } from '@/features/auth/components/RequireAuth';
import { AccountScope } from '@/features/scope/AccountScope';

import { LAZY_LOADING } from '../lazyCopy';

import { LazyBoundary } from './LazyBoundary';

/**
 * The admin screen's chunk (AC-33, AC-35). The only reference to `AdminPage` anywhere in the main
 * bundle is this dynamic import — a static import of it, of `AdminShell` or of `adminCopy.ts` from
 * any main-bundle module would pull the screen back into the main chunk.
 */
const AdminPage = lazy(() => import('./AdminPage'));

/**
 * The `/admin` route element (slice 4.1, plan §5). **This file is in the main bundle**, so it holds
 * only what must render before the chunk arrives:
 *
 * - `RequireAuth` — anonymous goes to `/login?next=%2Fadmin` (AC-32);
 * - `AccountScope` — the user id that roots the probe's key (AC-34);
 * - `LazyBoundary` — a chunk that fails to load (AC-33);
 * - `Suspense` — a `role="status"` *Loading…* while it loads (AC-33).
 */
export function AdminRoute(): React.JSX.Element {
  return (
    <RequireAuth>
      <AccountScope>
        {(userId) => (
          <LazyBoundary>
            <Suspense
              fallback={
                <p role="status" className="mb-10 text-slate-600">
                  {LAZY_LOADING}
                </p>
              }
            >
              <AdminPage userId={userId} />
            </Suspense>
          </LazyBoundary>
        )}
      </AccountScope>
    </RequireAuth>
  );
}
