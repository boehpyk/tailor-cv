import { COUNTS_HEADING } from '../adminCopy';

import type { AdminUserDetail } from '@/api/admin';

export interface AccountOverviewProps {
  readonly user: AdminUserDetail;
}

/**
 * One account's fields (email as `<h1>`, id, role read-only with its note, created, status) and
 * its seven counts as a `<dl>` (AC-33). Presentational.
 *
 * SKELETON (T19): the email heading and an empty counts list; the fields come in T21.
 */
export function AccountOverview({ user }: AccountOverviewProps): React.JSX.Element {
  return (
    <div>
      <h1 className="text-lg font-semibold text-slate-900">{user.email}</h1>
      <h2 className="mt-4 text-sm font-semibold text-slate-900">{COUNTS_HEADING}</h2>
      <dl />
    </div>
  );
}
