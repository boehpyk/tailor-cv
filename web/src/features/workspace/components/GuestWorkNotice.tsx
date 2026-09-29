/**
 * **Guest work is named, not hidden** (AC-42, OQ-5). In the account workspace: when this browser's
 * guest session still owns runs, *"This browser has N tailoring run(s) from before you signed in.
 * They aren't in your history and are deleted within 24 hours."* and a link to the newest
 * (`/runs/{id}`). Nothing when there are none, or when the guest list answers 401 — and that 401
 * never triggers a refresh (H-60, 2.1's I-48).
 *
 * It reads 1.3's **guest** run list (`useTailoringRuns`), which is not scoped: fetched with the
 * `tc_guest` cookie and **without** the bearer, whatever scope it renders in (AC-48).
 *
 * SKELETON (T29): a distinguishable stub; T31 builds it.
 */
export function GuestWorkNotice(): React.JSX.Element {
  return <p>GuestWorkNotice (skeleton)</p>;
}
