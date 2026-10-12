import { HistoryList } from './HistoryList';
import { PostingList } from './PostingList';
import { SavedCvList } from './SavedCvList';

export interface AccountContentProps {
  readonly actorId: string;
  readonly userId: string;
}

/**
 * The Content area of `/admin/users/:userId` (AC-26): three `<section>`s, each with its `<h2>` —
 * saved CVs, history, job postings. A presentational shell; each list is its own container.
 *
 * SKELETON (T11): composes the three lists without their sections and headings; T13 builds them.
 */
export function AccountContent({ actorId, userId }: AccountContentProps): React.JSX.Element {
  return (
    <>
      <SavedCvList actorId={actorId} userId={userId} />
      <HistoryList actorId={actorId} userId={userId} />
      <PostingList actorId={actorId} userId={userId} />
    </>
  );
}
