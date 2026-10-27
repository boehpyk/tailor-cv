import { Link, Outlet } from 'react-router';

import { FOOTER_HOW_IT_WORKS_LABEL } from './features/auth/authCopy';
import { AuthStatus } from './features/auth/components/AuthStatus';
import { SystemStatus } from './features/health/components/SystemStatus';

const focusRing =
  'rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600';

/**
 * The layout route.
 *
 * Since slice 1.4 `App` renders no product surface of its own: the header, then whichever page the
 * URL selects through `<Outlet />`, then the footer. The pages are in `router.tsx`. What stays here
 * is what every page shares.
 *
 * The header carries the wordmark — a link home **inside** the page's one `<h1>`, whose accessible
 * name stays exactly "TailorCraft" (the mark beside it is decorative and `aria-hidden`) — and
 * `AuthStatus` (slice 2.1, AC-42), the one `<nav>`. Below `sm` the signed-in nav is moved to a bar
 * fixed to the bottom of the viewport by its own `max-sm:` classes (web-design-refresh AC-11): one
 * DOM for every width, so `main` and the footer carry the bottom padding that keeps content clear
 * of it.
 *
 * System status lives in the `<footer>` (web-design-refresh OQ-5): it is diagnostic rather than part
 * of the task, so it sits in the `contentinfo` landmark after `main`, not inside it.
 *
 * **The layout passes no props.** Each page reads the server state it needs from TanStack Query, so
 * the shell is not a relay for data it never uses. Nothing here depends on the router except
 * `Outlet` and the two `Link`s; `Outlet` renders nothing outside a router rather than throwing.
 */
export function App(): React.JSX.Element {
  return (
    <div className="flex min-h-dvh flex-col bg-surface">
      {/* Outside `<main>` so it is the page's `banner` landmark (slice 2.3): a `<header>` inside
          `<main>` is only a section header. */}
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-x-8 gap-y-2 px-4 py-3">
          <h1 className="font-display text-lg font-bold tracking-tight text-slate-900">
            <Link to="/" className={`flex items-center gap-2.5 ${focusRing}`}>
              <span
                aria-hidden="true"
                className="flex size-7 items-center justify-center rounded-lg bg-brand-600"
              >
                <span className="size-4 rounded-sm border-[1.5px] border-dashed border-white" />
              </span>
              TailorCraft
            </Link>
          </h1>
          <AuthStatus />
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-4 pt-8 pb-24 sm:pb-10">
        <Outlet />
      </main>

      <footer className="border-t border-slate-200">
        {/* `pb-24` below `sm`: the footer is the last thing on the page, so it is what the fixed
            bottom bar would otherwise cover. */}
        <div className="mx-auto flex max-w-6xl flex-col gap-6 px-4 pt-8 pb-24 text-sm text-slate-600 sm:pb-8">
          <section aria-labelledby="system-status-heading">
            <h2
              id="system-status-heading"
              className="mb-3 text-xs font-semibold tracking-wide text-slate-600 uppercase"
            >
              System status
            </h2>
            <SystemStatus />
          </section>
          <p>
            <Link
              to="/welcome"
              className={`font-medium text-brand-700 underline underline-offset-2 max-sm:inline-flex max-sm:min-h-11 max-sm:items-center ${focusRing}`}
            >
              {FOOTER_HOW_IT_WORKS_LABEL}
            </Link>
          </p>
        </div>
      </footer>
    </div>
  );
}
