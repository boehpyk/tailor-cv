import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type RenderResult } from '@testing-library/react';
import { createMemoryRouter, RouterProvider, type DataRouter } from 'react-router';

import { routes } from '@/router';

import type { ReactElement, ReactNode } from 'react';

/**
 * Render a component inside a fresh query client.
 *
 * **Fresh per test, and with retries off.** A shared client would leak cache between tests, making
 * results depend on file order; retries on would make a test asserting an error state wait for
 * several attempts before the state it is asserting ever appears.
 */
export function renderWithQuery(ui: ReactElement): RenderResult {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }

  return render(ui, { wrapper: Wrapper });
}

export interface RenderWithRouterResult extends RenderResult {
  /** The memory router mounted for this render — read `.state.location.pathname` after a
   * navigation, rather than re-querying the DOM for a fact the router already knows. */
  readonly router: DataRouter;
}

export interface RenderWithRouterOptions {
  /** A caller-supplied client — for a test that needs to seed the cache before the first render, or
   * to inspect it afterwards. Defaults to a fresh one, matching `renderWithQuery`'s reasoning. */
  readonly queryClient?: QueryClient;
}

/**
 * Mount the **real route table** (`router.tsx`'s `routes`, the same table `main.tsx` gives the
 * browser router) on a `createMemoryRouter` at `path`, wrapped in a fresh `QueryClientProvider`.
 *
 * `App.test.tsx` did this by hand (`createMemoryRouter(routes, { initialEntries: ['/'] })` inside
 * its own `renderApp`) before this helper existed, because F3 made `App` the layout route: nothing
 * below `/` renders without a router to resolve it, so a test of "what a visitor to `/runs/{id}/cv`
 * sees" has to mount the table, not a bare component. This generalises that pattern to any path —
 * the workspace, a run page, a redirect, the not-found route — instead of every test file
 * reinventing `createMemoryRouter` with its own `initialEntries`.
 *
 * **Returns the router alongside the render result.** A test asserting a navigation happened (AC-25:
 * launch → `/runs/{id}`; AC-22: a deep link's redirect) needs to read *where the app ended up*, and
 * `router.state.location.pathname` is that fact stated directly by React Router — reading it is more
 * honest than inferring a navigation from which heading happens to be on screen, which would still
 * pass if the route matched by accident.
 */
export function renderWithRouter(
  path: string,
  opts: RenderWithRouterOptions = {},
): RenderWithRouterResult {
  const queryClient =
    opts.queryClient ??
    new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
  const router = createMemoryRouter(routes, { initialEntries: [path] });

  function Wrapper(): React.JSX.Element {
    return (
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
  }

  const result = render(<Wrapper />);
  return { ...result, router };
}
