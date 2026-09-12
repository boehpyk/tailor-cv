import { useEffect, useState } from 'react';

/** AC-29: after this long in one sub-state, say out loud that nothing is wrong. */
const STILL_WORKING_AFTER_SECONDS = 20;

const TICK_MS = 1000;

/**
 * Seconds since this component mounted, ticking once a second.
 *
 * **This is the one legitimate `useEffect` in the tailoring feature**, and it is worth seeing why it
 * qualifies when the poller next door does not. A `useEffect` is for synchronizing React with
 * something *outside* React. The run's status is server state — TanStack Query owns it, and a timer
 * here would be a second, worse copy of that. The wall clock is genuinely outside React: nothing
 * re-renders when a second passes unless something subscribes to it. So this effect subscribes
 * (`setInterval`), writes only local state, touches no server state, and unsubscribes on unmount.
 *
 * The count is computed from `Date.now()` rather than by adding one per tick. Browsers throttle
 * intervals in background tabs (to once a minute, eventually), so a tick counter would come back
 * from another tab claiming 3 seconds had passed after 3 minutes. The interval only decides *when*
 * to re-read the clock, never *how much* time went by.
 *
 * It measures how long **this page** has been waiting, not how long the run has existed. The server's
 * `started_at` would survive a refresh, but it is a different machine's clock: a client a minute
 * fast would open on "This is taking longer than usual" for a run that started a second ago.
 */
function useElapsedSeconds(): number {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  useEffect(() => {
    const mountedAt = Date.now();
    const timer = setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - mountedAt) / 1000));
    }, TICK_MS);
    return () => {
      clearInterval(timer);
    };
  }, []);

  return elapsedSeconds;
}

export interface TailoringProgressProps {
  /** Only the two non-terminal statuses reach this component; the container narrows first. */
  readonly status: 'queued' | 'running';
}

/**
 * The working state — presentational apart from its clock.
 *
 * **Two sentences for two situations (AC-29).** `queued` means nothing is happening yet — a worker
 * has not picked the run up, and if the worker is down (G-15) nothing ever will — so it says exactly
 * that. `running` means Gemini has the documents, and only then is an elapsed count meaningful.
 * Every word is driven by the run's real status; no timer pretends to be progress.
 *
 * **None of this copy is failure language, and there is no retry control** (AC-28). Working and
 * failed arrive seconds apart on the same polled resource, both as a 200; a user who cannot tell
 * them apart refreshes, and here a refresh can mean paying for a second call.
 *
 * The container renders this with a `key` of run id + status, so moving from `queued` to `running`
 * mounts a fresh instance and the count restarts — "Tailoring with Gemini… 3s" means three seconds
 * *of Gemini*, not of waiting for a worker. Resetting state with a `key` is the React way to say
 * "this is a different thing now"; an effect that watched `status` and zeroed the count would be
 * the effect-for-deriving pattern this feature avoids.
 *
 * `role="status"` makes it a polite live region, so the sub-state and the 20-second message are
 * announced when they change. The ticking count is `aria-hidden`: announcing it every second would
 * bury everything else a screen reader has to say.
 */
export function TailoringProgress({ status }: TailoringProgressProps): React.JSX.Element {
  const elapsedSeconds = useElapsedSeconds();

  return (
    <div
      role="status"
      className="space-y-1 rounded-md border border-slate-200 bg-slate-50 px-3 py-2"
    >
      {status === 'queued' ? (
        <p className="text-sm font-medium text-slate-800">Waiting for a worker…</p>
      ) : (
        <p className="text-sm font-medium text-slate-800">
          Tailoring with Gemini…{' '}
          <span aria-hidden="true" className="text-slate-500 tabular-nums">
            {elapsedSeconds}s
          </span>
        </p>
      )}

      {elapsedSeconds >= STILL_WORKING_AFTER_SECONDS && (
        <p className="text-sm text-slate-600">
          This is taking longer than usual — we are still working. Don&apos;t refresh.
        </p>
      )}
    </div>
  );
}
