import {
  COUNTS_HEADING,
  COUNT_LABELS,
  FIELD_CREATED,
  FIELD_ID,
  FIELD_ROLE,
  FIELD_STATUS,
  ROLE_LABELS,
  ROLE_READ_ONLY_NOTE,
  STATUS_ACTIVE,
  STATUS_DISABLED,
} from '../adminCopy';

import type { AccountCounts, AdminUserDetail } from '@/api/admin';

export interface AccountOverviewProps {
  readonly user: AdminUserDetail;
}

const TERM = 'text-slate-600';
const VALUE = 'font-medium text-slate-900 break-all';

/**
 * One account (AC-33): the email as `<h1>`, its fields, the role read-only with the sentence that
 * says why, and the seven counts as a `<dl>` — each `<dd>` straight after its `<dt>`.
 * Presentational.
 */
export function AccountOverview({ user }: AccountOverviewProps): React.JSX.Element {
  return (
    <div className="space-y-6">
      <h1 className="font-display text-xl font-bold break-all text-slate-900">{user.email}</h1>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
        <dt className={TERM}>{FIELD_ID}</dt>
        <dd className={VALUE}>{user.id}</dd>
        <dt className={TERM}>{FIELD_ROLE}</dt>
        <dd className={VALUE}>
          {ROLE_LABELS[user.role]}
          <p className="font-normal text-slate-600">{ROLE_READ_ONLY_NOTE}</p>
        </dd>
        <dt className={TERM}>{FIELD_CREATED}</dt>
        <dd className={VALUE}>{new Date(user.created_at).toLocaleDateString()}</dd>
        <dt className={TERM}>{FIELD_STATUS}</dt>
        <dd className={VALUE}>{user.disabled_at === null ? STATUS_ACTIVE : STATUS_DISABLED}</dd>
      </dl>
      <div>
        <h2 className="text-sm font-semibold text-slate-900">{COUNTS_HEADING}</h2>
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          {(Object.keys(COUNT_LABELS) as (keyof AccountCounts)[]).map((key) => (
            <div key={key} className="contents">
              <dt className={TERM}>{COUNT_LABELS[key]}</dt>
              <dd className={VALUE}>{user.counts[key]}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}
