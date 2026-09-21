import { useReadiness } from '../hooks/useReadiness';

import { GuestPurgeStatus } from './GuestPurgeStatus';

/**
 * The Phase 0 screen: the dependency report, rendered.
 *
 * It has no product value and it is not a placeholder either — it is the smallest component that
 * exercises the whole vertical (React → typed client → nginx → FastAPI → Postgres/Redis/Celery) and
 * the one that proves it. It is also where the three-states rule gets established before there is a
 * feature to be sloppy about: **loading, error and ready are rendered deliberately**, and they are
 * distinguished by a discriminated check rather than by a `loading` boolean sitting next to a `data`
 * field, which would let both render at once (CLAUDE.md).
 */
export function SystemStatus(): React.JSX.Element {
  const { data, isPending, isError, error } = useReadiness();

  if (isPending) {
    return <p className="text-sm text-slate-500">Checking dependencies…</p>;
  }

  if (isError) {
    // "The API could not be reached" is a different fact from "the API says Redis is down", and a
    // user or an operator needs to be able to tell them apart. Collapsing both into one red box is
    // the small dishonesty that makes a status page useless.
    return (
      <p className="text-sm text-red-700">
        Could not reach the API: {error instanceof Error ? error.message : 'unknown error'}
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <p className={data.ready ? 'font-medium text-green-700' : 'font-medium text-red-700'}>
        {data.ready ? 'All dependencies healthy' : 'Not ready'}
      </p>
      <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200">
        {Object.entries(data.dependencies).map(([name, status]) => (
          <li key={name} className="flex items-center justify-between px-4 py-2 text-sm">
            <span className="font-mono text-slate-700">{name}</span>
            <span className={status.healthy ? 'text-green-700' : 'text-red-700'}>
              {status.healthy ? 'up' : (status.detail ?? 'down')}
            </span>
          </li>
        ))}
      </ul>
      <section aria-labelledby="retention-heading" className="space-y-1">
        <h3 id="retention-heading" className="text-xs font-medium uppercase text-slate-400">
          Retention
        </h3>
        {/*
          The guarantee this product prints to users in four other places, rendered as either kept
          or not (AC-35). It is a second *reader* of the readiness cache entry this component
          already holds — no new query key, no second fetch (AC-37).

          Its loading and error states are the two branches above, deliberately: this component
          owns the query, so it is the only thing that knows whether a request is in flight or
          failed. "The API could not be reached" stays a different fact from "the API says the
          purge is stale", and collapsing them is the small dishonesty that makes a status page
          useless.

          `data.jobs?.guest_purge` is optional twice over, and the block renders "not reported"
          rather than crashing: during a deploy the browser can be served a bundle newer than the
          API (R-32).
        */}
        <GuestPurgeStatus job={data.jobs?.guest_purge} />
      </section>
    </div>
  );
}
