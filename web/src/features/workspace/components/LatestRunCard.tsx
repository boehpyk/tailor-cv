import { Link } from 'react-router';

import { failureCopyFor } from '@/features/tailoring/failureCopy';

import type { TailoringRunSummary } from '@/features/tailoring/types';

export interface LatestRunCardProps {
  /**
   * The session's newest run from the list (`latestTailoringRun(items)`), or `null` for a session
   * with none. The summary is enough: the card links to `/runs/{id}` and never shows a document.
   */
  readonly run: TailoringRunSummary | null;
}

const LINK_CLASS = 'font-medium text-slate-900 underline underline-offset-2';

/**
 * The latest run, as a way back to it — presentational (AC-25). One sentence per terminal-or-not
 * status, and a `<Link>` to the run's own page where there is something there to see.
 *
 * - *In progress → View* while the run is `queued` or `running`. The card does not poll and does
 *   not say how far along the run is: that is the run page's job, one click away, and a clock here
 *   without a poller behind it would be a number that stops being true.
 * - *Open your tailored documents* once it `succeeded` — the link *is* the sentence, so the one
 *   thing a returning user wants is the one thing to click.
 * - 1.3's failure copy when it `failed` (`failureCopy.ts`, the same words the run page shows). No
 *   **Try again** here: the launch control beside this card is the retry, and it is drawn from the
 *   checklist that knows whether the inputs are still usable — a second button that could only
 *   duplicate or contradict it would be one button too many.
 *
 * Renders nothing for a session with no run: an empty box labelled "latest run" says less than no
 * box at all.
 */
export function LatestRunCard({ run }: LatestRunCardProps): React.JSX.Element | null {
  if (run === null) {
    return null;
  }

  switch (run.status) {
    case 'queued':
    case 'running':
      return (
        <section
          aria-label="Your latest run"
          className="flex items-baseline justify-between gap-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-2"
        >
          <p className="text-sm text-slate-800">Your tailoring run is in progress.</p>
          <Link to={`/runs/${run.id}`} className={LINK_CLASS}>
            View
          </Link>
        </section>
      );
    case 'succeeded':
      return (
        <section
          aria-label="Your latest run"
          className="rounded-md border border-slate-200 bg-white px-3 py-2"
        >
          <p className="text-sm">
            <Link to={`/runs/${run.id}`} className={LINK_CLASS}>
              Open your tailored documents
            </Link>
          </p>
        </section>
      );
    case 'failed': {
      const copy = failureCopyFor(run.failure_reason);
      return (
        <section
          aria-label="Your latest run"
          className="space-y-1 rounded-md border border-amber-200 bg-amber-50 px-3 py-2"
        >
          <p className="text-xs font-medium tracking-wide text-amber-700 uppercase">
            Your last tailoring run did not finish
          </p>
          <p className="text-sm font-medium text-amber-900">{copy.headline}</p>
          {copy.hint !== null && <p className="text-sm text-amber-800">{copy.hint}</p>}
        </section>
      );
    }
  }
}
