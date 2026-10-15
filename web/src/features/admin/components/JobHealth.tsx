import { Fragment } from 'react';

import { useReadiness } from '@/features/health/hooks/useReadiness';

import {
  DEPENDENCY_HEALTHY,
  DEPENDENCY_LABELS,
  DEPENDENCY_UNHEALTHY,
  IDENTITY_SWEEP_LABEL,
  JOB_HEALTH_HEADING,
  NOT_REPORTED,
  OVERDUE_UNKNOWN,
  READINESS_ERROR,
  USER_LOADING,
  overdueLine,
} from '../adminCopy';

import { ErrorWithRetry } from './ContentPage';

const TERM = 'text-slate-600';
const VALUE = 'font-medium text-slate-900';

function labelOf(name: string): string {
  return name in DEPENDENCY_LABELS
    ? DEPENDENCY_LABELS[name as keyof typeof DEPENDENCY_LABELS]
    : name;
}

/**
 * Job health (AC-39): each dependency `/health/ready` reports (a 503 carries the report, so it is
 * data, not an error), the identity sweep's backlog, *Not reported by this API.* when `jobs` is
 * absent, an alert when the endpoint itself fails.
 */
export function JobHealth(): React.JSX.Element {
  const readiness = useReadiness();
  const { data } = readiness;
  const sweep = data?.jobs?.identity_token_sweep;
  return (
    <section aria-labelledby="admin-job-health-heading" className="space-y-3">
      <h2 id="admin-job-health-heading" className="text-base font-semibold text-slate-900">
        {JOB_HEALTH_HEADING}
      </h2>
      {data === undefined ? (
        readiness.error !== null ? (
          <ErrorWithRetry text={READINESS_ERROR} query={readiness} />
        ) : (
          <p role="status" className="text-sm text-slate-600">
            {USER_LOADING}
          </p>
        )
      ) : (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
          {Object.entries(data.dependencies).map(([name, dependency]) => (
            <Fragment key={name}>
              <dt className={TERM}>{labelOf(name)}</dt>
              <dd className={dependency.healthy ? VALUE : `${VALUE} text-red-700`}>
                {dependency.healthy ? DEPENDENCY_HEALTHY : DEPENDENCY_UNHEALTHY}
              </dd>
            </Fragment>
          ))}
          <dt className={TERM}>{IDENTITY_SWEEP_LABEL}</dt>
          <dd className={VALUE}>
            {sweep === undefined
              ? NOT_REPORTED
              : sweep.overdue === null
                ? OVERDUE_UNKNOWN
                : overdueLine(sweep.overdue)}
          </dd>
        </dl>
      )}
    </section>
  );
}
