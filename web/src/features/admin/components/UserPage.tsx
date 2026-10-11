import { useParams } from 'react-router';

import { USER_LOADING } from '../adminCopy';

export interface UserPageProps {
  readonly actorId: string;
}

/**
 * `/admin/users/:userId` (AC-33). Container: `useAdminUser(actorId, userId)`; views loading /
 * gone (`user_not_found`) / not_found (the firewall's code-less 404) / error / overview, with
 * `AccountOverview` and `AccountActions`.
 *
 * SKELETON (T19): a fixed loading status; no data yet (T21).
 */
export function UserPage({ actorId }: UserPageProps): React.JSX.Element {
  const { userId } = useParams();
  return (
    <p role="status" className="mb-10 text-slate-600" data-actor={actorId} data-user={userId ?? ''}>
      {USER_LOADING}
    </p>
  );
}
