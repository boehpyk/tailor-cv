import { Link } from 'react-router';

import { useTailoringRuns } from '@/features/tailoring/hooks/useTailoringRuns';
import { latestTailoringRun } from '@/features/tailoring/latestTailoringRun';

import { GUEST_WORK_LINK_LABEL, guestWorkNotice } from '../workspaceCopy';

/**
 * **Guest work is named, not hidden** (AC-42, OQ-5). In the account workspace: when this browser's
 * guest session still owns runs, say how many, that they are not in the history, and that they go
 * within 24 hours — and link to the newest (`/runs/{id}`, a guest route, because that is where it
 * lives). Signing in does not move them; 2.4's claim will.
 *
 * It reads 1.3's **guest** run list (`useTailoringRuns`), which is deliberately not scoped: it goes
 * to `/api/tailoring-runs` with the `tc_guest` cookie and **without** the bearer, whatever scope it
 * renders in (AC-48). That hook already folds a 401 `guest_session_expired` to "no runs", and a
 * guest request never triggers a refresh (H-60, 2.1's I-48) — so no session, no runs, a failed read
 * and an expired session all say the same thing: nothing.
 */
export function GuestWorkNotice(): React.JSX.Element | null {
  const runs = useTailoringRuns();
  const items = runs.data?.items ?? [];
  const newest = latestTailoringRun(items);
  if (newest === null) {
    return null;
  }

  return (
    <div className="mb-6 space-y-1 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm">
      <p className="text-amber-900">{guestWorkNotice(items.length)}</p>
      <Link
        to={`/runs/${encodeURIComponent(newest.id)}`}
        className="font-medium text-amber-900 underline underline-offset-2"
      >
        {GUEST_WORK_LINK_LABEL}
      </Link>
    </div>
  );
}
