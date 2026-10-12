import { useParams } from 'react-router';

import { CONTENT_LOADING } from '../adminCopy';

export interface AdminCvPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-32). */
  readonly actorId: string;
}

/**
 * `/admin/users/:userId/cvs/:baseCvId` (AC-27): one saved CV with its text. Views: loading / CV gone /
 * account gone / firewall 404 → `NotFoundPage` / error + Retry / page.
 *
 * SKELETON (T11): a distinguishable placeholder; T13 builds the views.
 */
export function AdminCvPage({ actorId }: AdminCvPageProps): React.JSX.Element {
  const { userId, baseCvId } = useParams();
  return (
    <p
      role="status"
      className="mb-10 text-slate-600"
      data-actor={actorId}
      data-user={userId ?? ''}
      data-item={baseCvId ?? ''}
    >
      {CONTENT_LOADING}
    </p>
  );
}
