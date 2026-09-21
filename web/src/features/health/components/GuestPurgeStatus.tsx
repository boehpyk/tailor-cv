import { describeAge, viewOfGuestPurge } from '../purgeView';

import type { GuestPurgeStatus as GuestPurgeJob } from '@/api/health';

/**
 * The Retention block of the status panel: is the 24-hour guest-data promise being kept?
 *
 * **Presentational, and deliberately so.** It takes the job object as a prop and has no hooks, no
 * query, no `useState` and no `useEffect` — `SystemStatus` already reads `readinessQueryKey`'s cache
 * entry (AC-37), and this block is a second *reader* of that one entry rather than a second fetch.
 * Being handed its data is what makes its four states trivially testable: a test supplies a prop
 * instead of standing up a query client and a stubbed `fetch`.
 *
 * **One branch point, choosing the sentence and the colour together.** The `switch` below is the
 * only place this component decides anything, and what it switches on is `viewOfGuestPurge`'s
 * verdict rather than the job's raw fields. That is the whole reason `PurgeView` exists (1.5's
 * lesson: a control that asked the *row* what a click meant while its copy came from the *view*
 * went on offering a download that 410'd for ever). Reintroducing an `if (job.stale)` here would
 * be a second derivation of a judgement that already has an owner.
 *
 * **Privacy (AC-36):** counts, an age and a status word. Nothing rendered here is a session id, a
 * filename, a storage key or a path, and nothing reachable from `PurgeView` can be one — `detail`,
 * the one free-text member of the wire type, is deliberately not carried onto the view at all (the
 * argument is in `purgeView.ts`). The test greps the *rendered output* rather than this source,
 * because a key would arrive in a prop and never appear here as text.
 */
export function GuestPurgeStatus({ job }: { job: GuestPurgeJob | undefined }): React.JSX.Element {
  const view = viewOfGuestPurge(job);

  switch (view.kind) {
    case 'unreported':
      // An API older than this bundle, which happens during a deploy (R-32). Not an error: nothing
      // failed. Saying "not reported" rather than showing a confident zero keeps this client from
      // inventing a fact it was never told.
      return <p className="text-sm text-slate-500">Guest purge: not reported by this API.</p>;

    case 'unscheduled':
      // `text-amber-700`, not red: during the rehearsal window this is the deliberate state
      // (ADR-0018 decision 5). It is still the sentence the whole flag exists to make visible — a
      // config file nobody re-reads is not a signal, and this promise is printed to users in four
      // other places.
      return (
        <p className="text-sm text-amber-700">
          Scheduled guest purge is <strong>off</strong> — guest data is deleted only when someone
          runs it by hand · {backlogPhrase(view.overdue)}
        </p>
      );

    case 'stale':
      return (
        <p className="text-sm text-red-700">
          Guest purge has not run for over 3 hours · {backlogPhrase(view.overdue)}
        </p>
      );

    case 'healthy':
      return (
        <p className="text-sm text-slate-500">
          {view.ageSeconds === null
            ? 'Guest purge is scheduled and healthy'
            : `Guest purge ran ${describeAge(view.ageSeconds)}`}{' '}
          · {backlogPhrase(view.overdue)}
        </p>
      );
  }
}

/**
 * The backlog clause. `null` reads as an explicit *unknown*, never as a dropped clause and never
 * as zero (R-29).
 *
 * Dropping it is the dangerous choice: an operator reading a sentence with no backlog in it infers
 * there is none. `overdue` is the number the runbook tells them to trust *over* the heartbeat —
 * because a heartbeat can be written by a job that is not working, while a count of what is still
 * expired cannot be faked by one — so "could not be counted" has to look different from "nothing
 * is waiting".
 */
function backlogPhrase(overdue: number | null): string {
  if (overdue === null) {
    return 'expired sessions waiting: unknown';
  }
  return `${String(overdue)} expired sessions waiting`;
}
