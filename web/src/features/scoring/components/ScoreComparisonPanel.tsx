import { useId } from 'react';

import { ApiError } from '@/api/client';

import {
  COMPARE_BUSY,
  COMPARE_BUTTON,
  COMPARE_HEADING,
  COMPARE_NOTICE,
  COMPARE_UNAVAILABLE,
  DRAFT_NOTE,
  LOADING,
  LOAD_ERROR,
  NOT_COMPARABLE,
  RETRY_LOAD,
  SIDE_FAILED,
  SIDE_LABELS,
  SIDE_SCORING,
  TRY_AGAIN,
  VERDICTS,
  deltaPoints,
  scorePercent,
} from '../fitScoreCopy';
import { useCompareScores } from '../hooks/useCompareScores';
import { useRunFitScores } from '../hooks/useRunFitScores';

import type { SideRequest } from '../hooks/useCompareScores';
import type { FitScore, FitScoreSide } from '../types';

export interface ScoreComparisonPanelProps {
  /** A succeeded account run (the caller renders this only for `succeeded`). */
  readonly runId: string;
}

const SIDES: readonly FitScoreSide[] = ['before', 'after'];

const BUTTON =
  'min-h-11 rounded-lg px-4 text-sm font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60 sm:min-h-9';

function isBusy(request: SideRequest): boolean {
  return (
    request.status === 'error' &&
    request.error instanceof ApiError &&
    request.error.code === 'scoring_busy'
  );
}

/**
 * Before and after tailoring (AC-18, AC-37), on an account's run page. The stored scores and the
 * verdict are the read's (`useRunFitScores`); the client never computes a verdict. *Compare scores*
 * sends only the sides not stored yet, in parallel, on a click only (each is a paid Gemini call).
 * Per side: scoring, stored, or failed with *Try again*; a busy scorer is said once, for the panel,
 * and *Compare scores* comes back so the user can try later.
 */
export function ScoreComparisonPanel({ runId }: ScoreComparisonPanelProps): React.JSX.Element {
  const headingId = useId();
  const noticeId = useId();
  const read = useRunFitScores(runId);
  const { compare, sides } = useCompareScores(runId);
  const { data } = read;

  let body: React.ReactNode;
  if (data === undefined) {
    body = read.isError ? (
      <div role="alert" className="space-y-2">
        <p className="text-sm text-danger-600">{LOAD_ERROR}</p>
        <button
          type="button"
          onClick={() => void read.refetch()}
          disabled={read.isFetching}
          className={`${BUTTON} border border-slate-300 bg-white text-slate-900`}
        >
          {RETRY_LOAD}
        </button>
      </div>
    ) : (
      <p role="status" className="text-sm text-slate-600">
        {LOADING}
      </p>
    );
  } else if (data.unavailable !== null) {
    body = <p className="text-sm text-slate-700">{COMPARE_UNAVAILABLE[data.unavailable]}</p>;
  } else {
    const missing = SIDES.filter((side) => data[side] === null);
    const anyPending = missing.some((side) => sides[side].status === 'pending');
    const busy = missing.some((side) => isBusy(sides[side]));
    const failed = (side: FitScoreSide): boolean =>
      sides[side].status === 'error' && !isBusy(sides[side]);
    const showCompare = missing.length > 0 && !anyPending && !missing.some(failed);
    body = (
      <>
        <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {SIDES.map((side) => (
            <div key={side} className="space-y-1 rounded-lg border border-slate-200 p-3">
              <dt className="text-sm font-medium text-slate-700">{SIDE_LABELS[side]}</dt>
              <dd className="space-y-2">
                <SideValue
                  stored={data[side]}
                  request={sides[side]}
                  failed={failed(side)}
                  onRetry={() => {
                    compare([side]);
                  }}
                />
              </dd>
            </div>
          ))}
        </dl>
        {data.after !== null && <p className="text-sm text-slate-600">{DRAFT_NOTE}</p>}
        <div role="status" className="text-sm text-slate-700">
          {busy ? COMPARE_BUSY : null}
        </div>
        {data.comparable === false && <p className="text-sm text-slate-700">{NOT_COMPARABLE}</p>}
        {data.comparable === true && data.comparison !== null && (
          <p className="text-sm text-slate-900">
            <span className="font-semibold tabular-nums">{deltaPoints(data.comparison.delta)}</span>{' '}
            <span>{VERDICTS[data.comparison.verdict]}</span>
          </p>
        )}
        {showCompare && (
          <div className="space-y-2">
            <p id={noticeId} className="text-sm text-slate-600">
              {COMPARE_NOTICE}
            </p>
            <button
              type="button"
              aria-describedby={noticeId}
              onClick={() => {
                compare(missing);
              }}
              className={`${BUTTON} bg-brand-600 text-white hover:bg-brand-700 max-sm:w-full`}
            >
              {COMPARE_BUTTON}
            </button>
          </div>
        )}
      </>
    );
  }

  return (
    <section aria-labelledby={headingId} className="space-y-4">
      <h2 id={headingId} className="font-display text-lg font-semibold text-slate-900">
        {COMPARE_HEADING}
      </h2>
      {body}
    </section>
  );
}

interface SideValueProps {
  readonly stored: FitScore | null;
  readonly request: SideRequest;
  readonly failed: boolean;
  readonly onRetry: () => void;
}

/** One side's value: its stored score wins; else its request's state. Busy is the panel's. */
function SideValue({ stored, request, failed, onRetry }: SideValueProps): React.JSX.Element {
  if (stored !== null) {
    return (
      <p className="text-2xl font-semibold tabular-nums text-slate-900">
        {scorePercent(stored.score_percent)}
      </p>
    );
  }
  if (request.status === 'pending') {
    return <p className="text-sm text-slate-600">{SIDE_SCORING}</p>;
  }
  if (failed) {
    return (
      <>
        <p className="text-sm text-danger-600">{SIDE_FAILED}</p>
        <button
          type="button"
          onClick={onRetry}
          className={`${BUTTON} border border-slate-300 bg-white text-slate-900`}
        >
          {TRY_AGAIN}
        </button>
      </>
    );
  }
  return <></>;
}
