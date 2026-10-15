import { Link } from 'react-router';

import {
  CONTENT_EMPTY,
  CONTENT_ERROR,
  CONTENT_HEADINGS,
  CONTENT_LOADING,
  POSTINGS_WINDOW_NOTE,
  UNTITLED_POSTING,
} from '../adminCopy';
import { contentLink } from '../contentView';
import { useAdminPostings } from '../hooks/useAccountContent';

import { ContentSection, EMPTY, ROW_LINK } from './ContentSection';

export interface PostingListProps {
  readonly actorId: string;
  /** The account whose content this lists. */
  readonly userId: string;
}

/** The server's window (`RECENT_POSTINGS_MAX`): a full list says it may not be all (OQ-6). */
const POSTINGS_WINDOW = 20;

/**
 * The account's 20 most recent job postings (AC-26, OQ-6): loading / error + Retry / *No job
 * postings.* / one row per posting, its title linking to the posting's page; *Showing the 20 most
 * recent.* when the window is full.
 */
export function PostingList({ actorId, userId }: PostingListProps): React.JSX.Element {
  const postings = useAdminPostings(actorId, userId);
  const items = postings.data?.items ?? [];
  return (
    <ContentSection
      id="admin-content-postings"
      heading={CONTENT_HEADINGS.postings}
      query={postings}
      loadingText={CONTENT_LOADING.postings}
      errorText={CONTENT_ERROR.postings}
    >
      {items.length === 0 ? (
        <p className={EMPTY}>{CONTENT_EMPTY.postings}</p>
      ) : (
        <>
          <ul className="space-y-2 text-sm">
            {items.map((p) => (
              <li key={p.id} className="flex flex-wrap gap-x-3">
                <Link
                  to={contentLink({ kind: 'user', userId }, 'postings', p.id)}
                  className={ROW_LINK}
                >
                  {p.title ?? UNTITLED_POSTING}
                </Link>
                <span className="text-slate-600">
                  {new Date(p.created_at).toLocaleDateString()}
                </span>
              </li>
            ))}
          </ul>
          {items.length >= POSTINGS_WINDOW && <p className={EMPTY}>{POSTINGS_WINDOW_NOTE}</p>}
        </>
      )}
    </ContentSection>
  );
}
