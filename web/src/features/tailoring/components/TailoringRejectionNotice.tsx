import { Link } from 'react-router';

export interface TailoringRejectionNoticeProps {
  /** Already mapped from the error's `code` (`rejectionMessage`) — never the server's `message`. */
  readonly message: string;
  /**
   * Present only for a 409 `tailoring_already_running` that named the active run. Attaching to
   * that run is the whole point of the 409 carrying its id: the user watches the run they already
   * paid for instead of being pushed towards paying again.
   */
  readonly onViewActiveRun: (() => void) | null;
  /**
   * The active run's app path, for a caller that offers a **link** rather than a button (slice
   * 2.3, AC-41: the account workspace links a 409 `tailoring_already_running` to `/history/{id}`).
   * It is a place to go, and a link can be opened in a new tab. Takes precedence over
   * `onViewActiveRun`; absent for every caller that predates it.
   */
  readonly activeRunHref?: string | null;
}

/**
 * **Error B — the API rejected the request** — presentational. Red, like the sibling panels' error
 * B, and worded as "we could not start", never as "your run failed": no run exists to have failed.
 *
 * A button rather than a link for "View the run in progress": there is no route to link to until
 * React Router arrives in 1.4, and what the control does is change which run this panel watches.
 */
export function TailoringRejectionNotice({
  message,
  onViewActiveRun,
  activeRunHref = null,
}: TailoringRejectionNoticeProps): React.JSX.Element {
  const controlClass =
    'inline-block rounded-md border border-red-300 bg-white px-3 py-1.5 text-sm font-medium text-red-700';
  return (
    <div role="alert" className="space-y-2 rounded-md border border-red-200 bg-red-50 px-3 py-2">
      <p className="text-sm font-medium text-red-700">{message}</p>
      {activeRunHref !== null ? (
        <Link to={activeRunHref} className={controlClass}>
          View the run in progress
        </Link>
      ) : (
        onViewActiveRun !== null && (
          <button type="button" onClick={onViewActiveRun} className={controlClass}>
            View the run in progress
          </button>
        )
      )}
    </div>
  );
}
