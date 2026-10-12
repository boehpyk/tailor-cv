import { useParams } from 'react-router';

import { CONTENT_LOADING } from '../adminCopy';

export interface AdminPostingPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-32). */
  readonly actorId: string;
}

/**
 * `/admin/users/:userId/postings/:postingId` (AC-25): one job posting — title, source, URL as
 * text, the body through `BodyText`.
 *
 * SKELETON (T11): a distinguishable placeholder; T13 builds the views.
 */
export function AdminPostingPage({ actorId }: AdminPostingPageProps): React.JSX.Element {
  const { userId, postingId } = useParams();
  return (
    <p
      role="status"
      className="mb-10 text-slate-600"
      data-actor={actorId}
      data-user={userId ?? ''}
      data-item={postingId ?? ''}
    >
      {CONTENT_LOADING}
    </p>
  );
}
