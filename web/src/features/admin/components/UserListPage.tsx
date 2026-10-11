import { USERS_HEADING } from '../adminCopy';

import { UserSearchForm } from './UserSearchForm';
import { UserTable } from './UserTable';

export interface UserListPageProps {
  readonly actorId: string;
}

/**
 * `/admin`: the user directory (AC-31, AC-32). Container: `?q=` from the URL,
 * `useUserDirectory(actorId, q)`, the erase notice from `location.state`.
 *
 * SKELETON (T19): the heading, an empty form and an empty table; no data yet (T21).
 */
export function UserListPage({ actorId }: UserListPageProps): React.JSX.Element {
  return (
    <section aria-labelledby="admin-users-heading" className="mb-10 space-y-4">
      <h1 id="admin-users-heading" className="text-lg font-semibold text-slate-900">
        {USERS_HEADING}
      </h1>
      <UserSearchForm
        q=""
        onSearch={() => {
          // T21: setSearchParams.
        }}
      />
      <UserTable users={[]} actorId={actorId} />
    </section>
  );
}
