import { useParams } from 'react-router';

import { CONTENT_LOADING } from '../adminCopy';

export interface AdminRunPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-32). */
  readonly actorId: string;
}

/**
 * `/admin/users/:userId/runs/:runId` (AC-28): status, posting link, CV label or *CV deleted*,
 * both documents with their downloads, the exports, the delete; *Refresh* while queued/running.
 *
 * SKELETON (T11): a distinguishable placeholder; T13 builds the views.
 */
export function AdminRunPage({ actorId }: AdminRunPageProps): React.JSX.Element {
  const { userId, runId } = useParams();
  return (
    <p
      role="status"
      className="mb-10 text-slate-600"
      data-actor={actorId}
      data-user={userId ?? ''}
      data-item={runId ?? ''}
    >
      {CONTENT_LOADING}
    </p>
  );
}
