import { ADMIN_EMPTY, ADMIN_HEADING } from '../adminCopy';

/**
 * The admin screen once access is confirmed (AC-31's 204). Presentational: no data, no hooks.
 * The `<h1>` labels the `<section>` (AC-36). Deliberately empty — 4.2 replaces the empty state
 * with the user list. Reached only through `AdminPage`'s chunk (AC-35).
 */
export function AdminShell(): React.JSX.Element {
  return (
    <section aria-labelledby="admin-heading" className="mb-10">
      <h1 id="admin-heading" className="text-lg font-semibold text-slate-900">
        {ADMIN_HEADING}
      </h1>
      <p className="mt-2 text-slate-600">{ADMIN_EMPTY}</p>
    </section>
  );
}
