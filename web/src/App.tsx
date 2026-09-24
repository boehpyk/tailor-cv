import { Outlet } from 'react-router';

import { AuthStatus } from './features/auth/components/AuthStatus';
import { SystemStatus } from './features/health/components/SystemStatus';

/**
 * The layout route.
 *
 * Since slice 1.4 `App` renders no product surface of its own: the header, then whichever page the
 * URL selects through `<Outlet />`, then the system status. The pages are in `router.tsx`. What
 * stays here is what every page shares — and system status stays last because it is diagnostic
 * rather than part of the task.
 *
 * The header carries `AuthStatus` (slice 2.1, AC-42) — the one piece of auth every page shows. It
 * reads `useAuth()` itself, so it is a child here, not a prop.
 *
 * **The layout passes no props.** Each page reads the server state it needs from TanStack Query, so
 * the shell is not a relay for data it never uses. Nothing here depends on the router except
 * `Outlet` itself, which renders nothing outside a router rather than throwing (react-router 7 reads
 * a context whose default `outlet` is `null`) — so the shell can still be rendered on its own.
 */
export function App(): React.JSX.Element {
  return (
    <main className="mx-auto max-w-2xl px-6 py-16">
      <header className="mb-10">
        <div className="flex flex-wrap items-baseline justify-between gap-4">
          <h1 className="text-3xl font-semibold tracking-tight text-slate-900">TailorCraft</h1>
          <AuthStatus />
        </div>
        <p className="mt-2 text-slate-600">
          Tailor your CV and cover letter to a job posting in under two minutes.
        </p>
      </header>

      <Outlet />

      <section aria-labelledby="system-status-heading">
        <h2
          id="system-status-heading"
          className="mb-3 text-sm font-medium text-slate-500 uppercase"
        >
          System status
        </h2>
        <SystemStatus />
      </section>
    </main>
  );
}
