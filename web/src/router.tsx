import { Navigate, createBrowserRouter } from 'react-router';

import { App } from './App';
import { NotFoundPage } from './features/tailoring/components/NotFoundPage';
import { RunPage } from './features/tailoring/components/RunPage';
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
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];

export const router = createBrowserRouter(routes);
