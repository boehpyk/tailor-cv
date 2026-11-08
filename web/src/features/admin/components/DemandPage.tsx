import { useSearchParams } from 'react-router';

import { ApiError } from '@/api/client';

import {
  DEMAND_BAD_RANGE,
  DEMAND_CAPTION,
  DEMAND_COUNTER_LABELS,
  DEMAND_DAY_COLUMN,
  DEMAND_EMPTY,
  DEMAND_ERROR,
  DEMAND_FROM_LABEL,
  DEMAND_HEADING,
  DEMAND_LOADING,
  DEMAND_TO_LABEL,
  DEMAND_TOTALS_ROW,
} from '../adminCopy';
import { useDemandCounters } from '../hooks/useDemandCounters';

import type { DemandCounts } from '@/api/admin';

export interface DemandPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key. */
  readonly actorId: string;
}

const COUNTERS = Object.keys(DEMAND_COUNTER_LABELS) as (keyof DemandCounts)[];

/** The default range: the last 30 UTC days, today included. */
const DEFAULT_DAYS = 30;

function isoDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

const INPUT =
  'min-h-11 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9';

/**
 * `/admin/demand` (AC-29, AC-39, ADR-0043): *From* / *To* native date inputs whose range lives in
 * the URL, then loading, error, empty (*Nothing counted in this range.*) or the table of days with a
 * totals row. Anonymous counts only: nothing here says who.
 */
export function DemandPage({ actorId }: DemandPageProps): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const today = new Date();
  const from =
    params.get('from') ?? isoDay(new Date(today.getTime() - (DEFAULT_DAYS - 1) * 86_400_000));
  const to = params.get('to') ?? isoDay(today);
  const counters = useDemandCounters(actorId, from, to);
  const { data } = counters;

  function setRange(key: 'from' | 'to', value: string): void {
    setParams({ from, to, [key]: value }, { replace: true });
  }

  let body: React.ReactNode;
  if (data === undefined) {
    body = counters.isError ? (
      <p role="alert" className="text-slate-700">
        {counters.error instanceof ApiError && counters.error.status === 422
          ? DEMAND_BAD_RANGE
          : DEMAND_ERROR}
      </p>
    ) : (
      <p role="status" className="text-slate-600">
        {DEMAND_LOADING}
      </p>
    );
  } else if (COUNTERS.every((key) => data.totals[key] === 0)) {
    body = <p className="text-slate-700">{DEMAND_EMPTY}</p>;
  } else {
    body = (
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="mb-2 text-left text-sm text-slate-600">{DEMAND_CAPTION}</caption>
          <thead className="border-b border-slate-200 text-slate-600">
            <tr>
              <th scope="col" className="py-2 pr-4 font-medium">
                {DEMAND_DAY_COLUMN}
              </th>
              {COUNTERS.map((key) => (
                <th key={key} scope="col" className="py-2 pr-4 font-medium">
                  {DEMAND_COUNTER_LABELS[key]}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.days.map(({ day, counts }) => (
              <tr key={day} className="border-b border-slate-100">
                <th scope="row" className="whitespace-nowrap py-2 pr-4 font-normal">
                  {day}
                </th>
                {COUNTERS.map((key) => (
                  <td key={key} className="py-2 pr-4 tabular-nums">
                    {counts[key]}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
          <tfoot className="font-semibold text-slate-900">
            <tr>
              <th scope="row" className="py-2 pr-4">
                {DEMAND_TOTALS_ROW}
              </th>
              {COUNTERS.map((key) => (
                <td key={key} className="py-2 pr-4 tabular-nums">
                  {data.totals[key]}
                </td>
              ))}
            </tr>
          </tfoot>
        </table>
      </div>
    );
  }

  return (
    <section aria-labelledby="admin-demand-heading" className="mb-10 min-w-0 space-y-6">
      <h1 id="admin-demand-heading" className="font-display text-xl font-bold text-slate-900">
        {DEMAND_HEADING}
      </h1>
      <div className="flex flex-wrap gap-4">
        <label className="flex flex-col gap-1 text-sm text-slate-700">
          {DEMAND_FROM_LABEL}
          <input
            type="date"
            value={from}
            onChange={(event) => {
              setRange('from', event.target.value);
            }}
            className={INPUT}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm text-slate-700">
          {DEMAND_TO_LABEL}
          <input
            type="date"
            value={to}
            onChange={(event) => {
              setRange('to', event.target.value);
            }}
            className={INPUT}
          />
        </label>
      </div>
      {body}
    </section>
  );
}
