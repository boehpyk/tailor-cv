import { Link } from 'react-router';

import {
  COLUMN_CREATED,
  COLUMN_EMAIL,
  COLUMN_ROLE,
  COLUMN_STATUS,
  ROLE_LABELS,
  STATUS_ACTIVE,
  STATUS_DISABLED,
  USERS_CAPTION,
  YOU_MARKER,
} from '../adminCopy';

import type { AdminUserSummary } from '@/api/admin';

export interface UserTableProps {
  readonly users: readonly AdminUserSummary[];
  /** The signed-in admin: their own row is marked *You* (OQ-18). */
  readonly actorId: string;
}

const BADGE = 'ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700';

/**
 * The directory's table (AC-31, AC-39): a caption, four columns, the email linking to the
 * account's page. Presentational.
 */
export function UserTable({ users, actorId }: UserTableProps): React.JSX.Element {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{USERS_CAPTION}</caption>
        <thead className="border-b border-slate-200 text-slate-600">
          <tr>
            <th scope="col" className="py-2 pr-4 font-medium">
              {COLUMN_EMAIL}
            </th>
            <th scope="col" className="py-2 pr-4 font-medium">
              {COLUMN_ROLE}
            </th>
            <th scope="col" className="py-2 pr-4 font-medium">
              {COLUMN_STATUS}
            </th>
            <th scope="col" className="py-2 font-medium">
              {COLUMN_CREATED}
            </th>
          </tr>
        </thead>
        <tbody>
          {users.map((user) => (
            <tr key={user.id} className="border-b border-slate-100">
              <td className="py-2 pr-4 break-all">
                <Link
                  to={`/admin/users/${encodeURIComponent(user.id)}`}
                  className="font-medium text-slate-900 underline underline-offset-2"
                >
                  {user.email}
                </Link>
                {user.id === actorId && <span className={BADGE}>{YOU_MARKER}</span>}
              </td>
              <td className="py-2 pr-4">{ROLE_LABELS[user.role]}</td>
              <td className="py-2 pr-4">
                {user.disabled_at === null ? STATUS_ACTIVE : STATUS_DISABLED}
              </td>
              <td className="py-2">{new Date(user.created_at).toLocaleDateString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
