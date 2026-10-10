import { Navigate, createBrowserRouter } from 'react-router';

import { App } from './App';
import { AdminRoute } from './features/admin/components/AdminRoute';
import { ConfirmEmailPage } from './features/accountMail/components/ConfirmEmailPage';
import { PasswordResetConfirmPage } from './features/accountMail/components/PasswordResetConfirmPage';
import { PasswordResetRequestPage } from './features/accountMail/components/PasswordResetRequestPage';
import { AccountPage } from './features/auth/components/AccountPage';
import { LoginPage } from './features/auth/components/LoginPage';
import { RegisterPage } from './features/auth/components/RegisterPage';
import { RequireAuth } from './features/auth/components/RequireAuth';
import { HistoryPage } from './features/history/components/HistoryPage';
import { AccountScope } from './features/scope/AccountScope';
import { NotFoundPage } from './features/tailoring/components/NotFoundPage';
import { RunPage } from './features/tailoring/components/RunPage';
import { BoardPage } from './features/tracking/components/BoardPage';
import { WorkspacePage } from './features/workspace/components/WorkspacePage';

import type { RouteObject } from 'react-router';

/**
 * The route table (technical plan §4). The URL owns "which run" and "which document", so nothing
 * in the tree keeps a `chosenRunId` in state any more: a deep link, a refresh and the back button
 * all read the same source.
 *
 * | Route                     | Renders                       |
 * |---------------------------|-------------------------------|
 * | `/`                       | `WorkspacePage`               |
 * | `/runs/:runId`            | redirect → `/runs/:runId/cv`  |
 * | `/runs/:runId/:document`  | `RunPage`                     |
 * | `/login`                  | `LoginPage`                   |
 * | `/register`               | `RegisterPage`                |
 * | `/confirm-email`          | `ConfirmEmailPage` (2.5)      |
 * | `/reset-password`         | `PasswordResetRequestPage` (2.5) |
 * | `/reset-password/confirm` | `PasswordResetConfirmPage` (2.5) |
 * | `/account`                | `RequireAuth` → `AccountPage` |
 * | `/history`                | `RequireAuth` → `AccountScope` → `HistoryPage` (2.3) |
 * | `/history/:runId`         | redirect → `/history/:runId/cv` (2.3)                 |
 * | `/history/:runId/:document` | `RequireAuth` → `AccountScope` → `RunPage` (2.3)    |
 * | `/board`                  | `RequireAuth` → `AccountScope` → `BoardPage` (3.1)    |
 * | `/admin`                  | `AdminRoute`: `RequireAuth` → `AccountScope` → lazy `AdminPage` (4.1) |
 * | `*`                       | `NotFoundPage` (E-28)         |
 *
 * `App` is the layout route: every page renders through its `<Outlet />`, between the header and
 * the system status.
 *
 * `/runs/:runId` on its own means "the run", and the run page always shows one document, so the
 * bare URL lands on the CV. `to="cv"` is resolved against the **route** (`runs/:runId`), not the
 * URL, so it keeps the id; and `replace` swaps the history entry instead of pushing one, which is
 * what keeps the back button honest — pressing it from `/runs/{id}/cv` returns to wherever the user
 * came from, not to a URL that would only bounce them forward again (AC-22).
 *
 * **Only `/account` is guarded** (slice 2.1). Every earlier route stays open: the product works
 * for a guest, and a guard there would turn "not logged in" into "cannot use TailorCraft". `/login`
 * and `/register` guard themselves the other way round — an authenticated visitor is sent on to
 * `safeNext(next)` by the page — so they need no wrapper here. Slice 2.5's three mail-link pages are
 * public too, and *not* turned away when signed in: a link opened while signed in as someone else
 * still confirms, and the current session is untouched (V-65).
 *
 * **Slice 2.3: the route decides the scope** (plan §0.9, AC-48). `/runs/…` renders with no
 * provider — the guest scope, the context's default — and `/history/…` inside `AccountScope`, so
 * the same `RunPage` reads and writes `/api/me/…` with the bearer there. `/` decides by the auth
 * state, inside `WorkspacePage`'s gate. Nothing asks "is someone signed in?" at request time.
 *
 * Exported separately from the browser router so a test can mount the same table on a
 * `createMemoryRouter` at any path.
 */
export const routes: RouteObject[] = [
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <WorkspacePage /> },
      {
        path: 'runs/:runId',
        children: [
          { index: true, element: <Navigate to="cv" replace /> },
          { path: ':document', element: <RunPage /> },
        ],
      },
      { path: 'login', element: <LoginPage /> },
      { path: 'register', element: <RegisterPage /> },
      { path: 'confirm-email', element: <ConfirmEmailPage /> },
      {
        path: 'reset-password',
        children: [
          { index: true, element: <PasswordResetRequestPage /> },
          { path: 'confirm', element: <PasswordResetConfirmPage /> },
        ],
      },
      {
        path: 'account',
        element: (
          <RequireAuth>
            <AccountPage />
          </RequireAuth>
        ),
      },
      {
        path: 'history',
        children: [
          {
            index: true,
            element: (
              <RequireAuth>
                <AccountScope>{(userId) => <HistoryPage userId={userId} />}</AccountScope>
              </RequireAuth>
            ),
          },
          {
            path: ':runId',
            children: [
              { index: true, element: <Navigate to="cv" replace /> },
              {
                path: ':document',
                element: (
                  <RequireAuth>
                    <AccountScope>{() => <RunPage />}</AccountScope>
                  </RequireAuth>
                ),
              },
            ],
          },
        ],
      },
      {
        path: 'board',
        element: (
          <RequireAuth>
            <AccountScope>{(userId) => <BoardPage userId={userId} />}</AccountScope>
          </RequireAuth>
        ),
      },
      { path: 'admin', element: <AdminRoute /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];

export const router = createBrowserRouter(routes);
