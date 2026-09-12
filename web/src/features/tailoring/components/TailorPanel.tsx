import { useBaseCvs } from '@/features/intake/hooks/useBaseCvs';
import { useJobPostings } from '@/features/posting/hooks/useJobPostings';

import { useCreateTailoringRun } from '../hooks/useCreateTailoringRun';
import { useTailoringRun } from '../hooks/useTailoringRun';
import { useTailoringRuns } from '../hooks/useTailoringRuns';

/**
 * The tailoring surface — a **container**, and today a deliberately inert one.
 *
 * **This is the T40 SKELETON, not an implementation.** It renders one fixed placeholder and nothing
 * else: no copy, no live-region roles, no button, and no branch on query state, on `status` or on
 * `failure_reason`. That is on purpose. In slice 1.1 the equivalent skeleton was a working component,
 * so the red tests written against it passed on arrival and proved nothing. T41 writes the failing
 * tests against this shell; T42 fills it in and must not touch those tests to get them green.
 *
 * **The signature is final: no props.** It reads the two prerequisites itself, like its two sibling
 * panels, rather than being handed them by `App`:
 *
 * - **The base CV and the job posting are server state**, so they are read from TanStack Query where
 *   they are needed. `useBaseCvs` and `useJobPostings` use the same query keys the other two panels
 *   use, so this is one cache and one request per list, not three. Passing them down from `App` would
 *   make `App` a container reading two queries only to forward them.
 * - **Props could not tell "still loading" apart from "there is none".** An `id: string | null` prop
 *   is `null` both before the list settles and after it settles empty, so the launch control would
 *   flash "add a base CV" at a user who has one. Handling that properly would mean passing the query
 *   state itself, which is just rebuilding `useQuery`'s return value as props.
 * - **The prerequisite is more than an id.** A base CV that is not `extracted` gets a 409
 *   `base_cv_not_extracted`, so the checklist needs the CV itself. T42 has to pick "the base CV" and
 *   "the job posting" by **the same rule the sibling panels use to decide what to show** (newest by
 *   timestamp), or this panel would tailor a different CV from the one on screen.
 *
 * **Hooks are called but not used yet.** Calling them exercises the provider, the imports and the
 * types without letting any state reach the markup. `useTailoringRun` gets `null` (a disabled query
 * that issues no request): which run to watch comes from the list's newest item, or from a 409's
 * `active_tailoring_run_id`, and choosing between those is T42's job.
 */
export function TailorPanel(): React.JSX.Element {
  useBaseCvs();
  useJobPostings();
  useTailoringRuns();
  useCreateTailoringRun();
  useTailoringRun(null);

  return (
    <div>
      <p>Placeholder (T40 skeleton).</p>
    </div>
  );
}
