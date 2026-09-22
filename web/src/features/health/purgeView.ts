/**
 * The guest purge's reading — the pure derivation the Retention block is built on.
 *
 * `viewOfGuestPurge` takes the job object `/health/ready` reported (server state, held by TanStack
 * Query under `readinessQueryKey`) and returns one of four mutually exclusive readings. It holds
 * nothing, fetches nothing and reads no wall clock, which is what lets AC-35's copy table drive it
 * row by row — the same shape as `features/export/exportView.ts` and `features/tailoring/runView.ts`.
 */

import type { GuestPurgeStatus } from '@/api/health';

/**
 * What the Retention block shows right now — four readings, so a discriminated union.
 *
 * A `stale` boolean sitting beside an `overdue` number would let the block render *"Guest purge has
 * not run for over 3 hours"* and *"Scheduled guest purge is off"* in the same frame, and eventually
 * it would: the two are answers to different questions and only one of them is the operator's next
 * action. Each member carries exactly what its sentence needs, already narrowed.
 *
 * **The judgement lives here and only here.** `GuestPurgeStatus.tsx` switches on `kind` once, for
 * both its sentence and its colour. This is the 1.5 lesson applied before it can bite: a control
 * that asked the *row* what a click meant, while the copy came from the *view*, went on offering a
 * download that 410'd for ever. Two derivations of one judgement drift; one cannot.
 *
 * **`detail` is deliberately absent from every member.** It is a free-text field carrying an
 * exception *type* from the server, and three arguments say not to render it:
 *
 * 1. It answers nothing a reader of this panel can act on — `TimeoutError` is vendor vocabulary,
 *    not an instruction.
 * 2. The only thing it signals is "a field above could not be computed", and that fact is already
 *    carried structurally by `overdue: null`, which the copy states in words. Rendering `detail`
 *    would be a second, unstructured statement of the same fact — the very duplication this union
 *    exists to prevent.
 * 3. AC-36 allows counts, an instant and a status word. `detail` is the one field in this payload
 *    whose contents nothing on the client constrains; the server promises an exception type today,
 *    and the type system promises only `string`. Leaving it off the view makes it *unrenderable*
 *    rather than merely unrendered, which is a guarantee instead of a habit.
 */
export type PurgeView =
  /**
   * The response carried no `jobs.guest_purge` member — an API older than this bundle (R-32).
   *
   * Not an error: nothing failed, this deployment simply does not report the job. The copy says so
   * ("not reported by this API") rather than showing a confident zero, which would be this client
   * inventing a fact.
   */
  | { readonly kind: 'unreported' }
  /**
   * Scheduled and not stale — the boring, correct state.
   *
   * **`ageSeconds` is nullable, and that is the wire's doing rather than this union's.** The server
   * computes `stale = scheduled && (last_run is null || age > 3h)`, so a *healthy* reading with no
   * `last_run` is unreachable in practice — which is exactly why a `!` here would be tempting and
   * wrong. It would be a client-side assertion of a server-side invariant (Constitution §4.5),
   * silently false the day that predicate changes, and its failure mode is *"ran NaN minutes ago"*
   * in an operator's browser. The precedent is literal: `ExportView.ready.byteSize` and
   * `RunView.failed.reason` are nullable for the same reason and degrade their copy the same way.
   *
   * So the copy degrades: the *"ran 12 minutes ago"* clause is dropped and the sentence states what
   * is actually known — that the purge is scheduled and healthy.
   */
  | {
      readonly kind: 'healthy';
      readonly ageSeconds: number | null;
      readonly overdue: number | null;
    }
  /**
   * `scheduled: false` — nothing fires this job; guest data goes only when someone runs it by hand.
   *
   * A **warning**, not an error (`text-amber-700`), because during slice 1.6's rehearsal window it
   * is the deliberate state. It carries no age: how long ago the last *manual* run was is not the
   * fact that matters when the answer is "there is no schedule".
   */
  | { readonly kind: 'unscheduled'; readonly overdue: number | null }
  /**
   * Scheduled, but no heartbeat for over three hours — the schedule fires hourly, so three misses
   * is a real fault. The one reading that is red.
   *
   * `ageSeconds` is nullable here for a *reachable* reason, unlike `healthy`'s: the server marks a
   * job that has **never** run as stale, and such a job has no `last_run` at all.
   */
  | {
      readonly kind: 'stale';
      readonly ageSeconds: number | null;
      readonly overdue: number | null;
    };

/**
 * Decide the Retention block's reading from the job the API reported. Pure — no hooks, no state, no
 * JSX, no `Date.now()`.
 *
 * ## The order of the branches is the specification
 *
 * 1. **`undefined` → `unreported`.** Taking the whole `GuestPurgeStatus | undefined` rather than
 *    four loose fields (as the task list writes it) is what lets the empty state be *a member of
 *    this union* instead of an `if` in the component sitting beside a `switch`. The component then
 *    has one branch point for four states, which is the property AC-35 is really asking for.
 * 2. **`scheduled === false` → `unscheduled`, ahead of `stale`.** The server guarantees
 *    `scheduled: false ⇒ stale: false` (AC-33), so the combination cannot arrive — but this
 *    function is total over the wire type and something must be decided for it. "Off" wins, because
 *    it is both the more actionable fact and the one that makes the staleness verdict meaningless:
 *    a job nothing schedules cannot be behind schedule.
 *
 *    The cost of the other order is not hypothetical and lands on *this* release:
 *    `GUEST_PURGE_ENABLED` ships false until the rehearsal earns the flip, so swapping these two
 *    branches would put a red *"has not run for over 3 hours"* alarm on every page load for the
 *    entire pre-rehearsal window — the exact failure AC-33 prevents on the server, reintroduced
 *    here. A test pins it.
 * 3. **`stale === true` → `stale`.** Read from the server, never re-derived here: the three-hour
 *    threshold is the API's rule and re-computing it from `last_run_age_seconds` would be a second
 *    authority that is silently wrong the day the threshold moves (Constitution §4.5).
 * 4. Otherwise **`healthy`**.
 *
 * ## `overdue: null` is carried, never smoothed over
 *
 * The count can fail while the response stays 200 (R-29). Every reading that shows a backlog
 * therefore takes `number | null`, and the `null` is rendered as an explicit *unknown* rather than
 * dropped. Dropping the clause would be the dangerous choice: an operator reading a sentence with
 * no backlog in it infers there is none, and `overdue` is precisely the field the runbook tells
 * them to trust over the heartbeat, because a heartbeat can be written by a job that is not
 * working. A number that could not be taken must look different from zero.
 *
 * @param job the `jobs.guest_purge` member of the readiness response, or `undefined` when the API
 *   that answered does not report it
 */
export function viewOfGuestPurge(job: GuestPurgeStatus | undefined): PurgeView {
  if (job === undefined) {
    return { kind: 'unreported' };
  }
  if (!job.scheduled) {
    return { kind: 'unscheduled', overdue: job.overdue };
  }
  if (job.stale) {
    return { kind: 'stale', ageSeconds: job.last_run_age_seconds, overdue: job.overdue };
  }
  return { kind: 'healthy', ageSeconds: job.last_run_age_seconds, overdue: job.overdue };
}

/** Whole units, largest that fits. Ordered longest-first so the first match wins. */
const AGE_UNITS: readonly (readonly [seconds: number, name: string])[] = [
  [86_400, 'day'],
  [3_600, 'hour'],
  [60, 'minute'],
  [1, 'second'],
];

/**
 * An age in seconds as the phrase the copy reads: *"12 minutes ago"*.
 *
 * Formatting rather than judgement, which is why it is a second exported function instead of a
 * field on `PurgeView`: the view says *what is true*, this says *how to say it*, and a test of the
 * truth table should not have to restate English.
 *
 * Whole units, largest that fits, singular where the count is one. It never returns a bare number
 * and never a timestamp — an instant is allowed by AC-36, but an operator reading a status panel
 * wants "how long ago", not a UTC string they have to subtract in their head.
 *
 * An age below one second reads as *"0 seconds ago"* rather than "just now": this panel is read by
 * someone deciding whether a background job is working, and a phrase that sounds like a judgement
 * ("just now") is doing more than reporting a number.
 */
export function describeAge(ageSeconds: number): string {
  const seconds = Math.max(0, Math.floor(ageSeconds));
  const [unitSeconds, unitName] = AGE_UNITS.find(([size]) => seconds >= size) ?? [1, 'second'];
  const count = Math.floor(seconds / unitSeconds);
  return `${String(count)} ${unitName}${count === 1 ? '' : 's'} ago`;
}
