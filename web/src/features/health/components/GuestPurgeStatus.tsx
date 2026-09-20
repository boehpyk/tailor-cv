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
 * **Privacy (AC-36):** counts, an age and a status word. Nothing rendered here is a session id, a
 * filename, a storage key or a path, and nothing in the props can be one — `GuestPurgeStatus` on
 * the wire carries no such field, and `detail`, the one free-text member, is deliberately not
 * carried onto `PurgeView` at all (the argument is in `purgeView.ts`).
 *
 * ---
 *
 * **SKELETON (T35) — this is not the finished component.**
 *
 * Four states, each rendering a distinguishable stub so that T36's tests fail on their assertions
 * rather than on an absent file. The copy, the Tailwind vocabulary (`text-sm`, `text-slate-500`,
 * `text-red-700` for stale, `text-amber-700` for *not scheduled*) and the *Retention* sub-section
 * that `SystemStatus` will wrap it in all arrive at T37.
 *
 * **The branching below is scaffolding and T37 deletes it.** It reads `job`'s fields directly so
 * that the four stubs are reachable while `viewOfGuestPurge` still throws. The shipped component is
 * a single `switch (viewOfGuestPurge(job).kind)` — one branch point, choosing the sentence and the
 * colour together — because the whole reason `PurgeView` exists is that this judgement is made in
 * exactly one place. Do not grow this version; replace it.
 */
export function GuestPurgeStatus({ job }: { job: GuestPurgeJob | undefined }): React.JSX.Element {
  if (job === undefined) {
    return <p>stub:unreported</p>;
  }
  if (!job.scheduled) {
    return <p>stub:unscheduled</p>;
  }
  if (job.stale) {
    return <p>stub:stale</p>;
  }
  return <p>stub:healthy</p>;
}
