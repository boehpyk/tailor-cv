import {
  COLUMN_CREATED,
  COLUMN_EMAIL,
  COLUMN_ROLE,
  COLUMN_STATUS,
  USERS_CAPTION,
} from '../adminCopy';

import type { AdminUserSummary } from '@/api/admin';

export interface UserTableProps {
  readonly users: readonly AdminUserSummary[];
  /** The signed-in admin: their own row is marked *You* (OQ-18). */
  readonly actorId: string;
}

/**
 * The directory's table (AC-31, AC-39): caption, four columns, a row link to `users/{id}`.
 * Presentational.
 *
 * SKELETON (T19): caption and header only; rows come in T21.
 */
export function UserTable({ users, actorId }: UserTableProps): React.JSX.Element {
  return (
    <table className="w-full text-left text-sm" data-rows={users.length} data-actor={actorId}>
      <caption className="sr-only">{USERS_CAPTION}</caption>
      <thead>
        <tr>
          <th scope="col">{COLUMN_EMAIL}</th>
          <th scope="col">{COLUMN_ROLE}</th>
          <th scope="col">{COLUMN_STATUS}</th>
          <th scope="col">{COLUMN_CREATED}</th>
        </tr>
      </thead>
      <tbody />
    </table>
  );
}
