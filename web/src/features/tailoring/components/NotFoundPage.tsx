import { Link } from 'react-router';

/**
 * E-28: the `*` route. Any path the router does not know renders this, never a blank page.
 *
 * The way home is a `<Link>`, not an `<a href="/">`: an anchor would ask the server for a fresh
 * document — a full reload, a new bundle, and every query cache emptied — where a client-side
 * navigation keeps the base CV, the posting and the run list already in TanStack Query. That is
 * also why this component needs a router above it: `Link` throws outside one.
 */
export function NotFoundPage(): React.JSX.Element {
  return (
    <section aria-labelledby="not-found-heading" className="mb-10">
      <h2 id="not-found-heading" className="text-lg font-semibold text-slate-900">
        We couldn&apos;t find that page.
      </h2>
      <p className="mt-2 text-slate-600">
        <Link to="/" className="font-medium text-slate-900 underline underline-offset-2">
          Back to the workspace
        </Link>
      </p>
    </section>
  );
}
