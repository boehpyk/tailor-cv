import { summarizeGuestWork } from '../guestWorkSummary';

import type { GuestWorkSummary } from '../guestWorkSummary';

/**
 * This browser's guest work, for the claim offer (AC-37) — or `null` when there is nothing to
 * offer, **including** while the guest lists load and when either read failed: an offer is not a
 * page, so it has no loading or error state of its own (technical plan §7, C-37).
 *
 * Composes 1.1's `useBaseCvs` and 1.3's `useTailoringRuns` — the **guest** lists, deliberately
 * unscoped: they go to `/api/base-cvs` and `/api/tailoring-runs` with the `tc_guest` cookie and
 * **without** the bearer, whatever scope renders the offer (2.3's `GuestWorkNotice` rule). Both
 * already fold a 401 `guest_session_expired` into "none", and a guest request never refreshes a
 * login (2.1's I-48), so an expired session costs no refresh call.
 *
 * SKELETON (T29): reads nothing and returns `summarizeGuestWork`'s skeleton sentinel; T31 wires the
 * two queries.
 */
export function useGuestWorkSummary(): GuestWorkSummary | null {
  return summarizeGuestWork([], []);
}
