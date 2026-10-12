import { HistoryList } from './HistoryList';
import { PostingList } from './PostingList';
import { SavedCvList } from './SavedCvList';

export interface AccountContentProps {
  readonly actorId: string;
  readonly userId: string;
}

/**
 * The Content area of `/admin/users/:userId` (AC-26): saved CVs, history and job postings, each
 * its own `<section>` with its `<h2>` and its own query. Presentational: no data of its own.
 */
export function AccountContent({ actorId, userId }: AccountContentProps): React.JSX.Element {
  return (
    <div className="mt-10 space-y-8">
      <SavedCvList actorId={actorId} userId={userId} />
      <HistoryList actorId={actorId} userId={userId} />
      <PostingList actorId={actorId} userId={userId} />
    </div>
  );
}
