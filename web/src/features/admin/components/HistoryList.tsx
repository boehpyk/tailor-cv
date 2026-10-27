import { Link } from 'react-router';

import {
  CONTENT_EMPTY,
  CONTENT_ERROR,
  CONTENT_HEADINGS,
  CONTENT_LOADING,
  CV_DELETED_NOTE,
  LOADING_MORE_LABEL,
  LOAD_MORE_LABEL,
  POSTING_MISSING_NOTE,
  UNTITLED_POSTING,
  historyStatusCopy,
} from '../adminCopy';
import { contentLink } from '../contentView';
import { useAdminHistory } from '../hooks/useAccountContent';

import { ContentSection, EMPTY, ROW_LINK } from './ContentSection';

import type { HistoryEntry } from '@/features/history/types';

export interface HistoryListProps {
  readonly actorId: string;
  /** The account whose content this lists. */
  readonly userId: string;
}

/** An entry's name: its posting's title, the untitled wording, or the missing-posting note. */
function entryName(entry: Pick<HistoryEntry, 'posting'>): string {
  if (entry.posting === null) return POSTING_MISSING_NOTE;
  return entry.posting.title ?? UNTITLED_POSTING;
}

/**
 * The account's history (AC-26): loading / error + Retry / *No tailoring history.* / one row per
 * run, its posting's title linking to the run's page, with status, date and CV. *Load more* while
 * the server gives a `next_cursor`, disabled while the next page is fetching.
 */
export function HistoryList({ actorId, userId }: HistoryListProps): React.JSX.Element {
  const history = useAdminHistory(actorId, userId);
  const entries = history.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <ContentSection
      id="admin-content-history"
      heading={CONTENT_HEADINGS.history}
      query={history}
      loadingText={CONTENT_LOADING.history}
      errorText={CONTENT_ERROR.history}
    >
      {entries.length === 0 ? (
        <p className={EMPTY}>{CONTENT_EMPTY.history}</p>
      ) : (
        <ul className="space-y-2 text-sm">
          {entries.map((entry) => (
            <li key={entry.id} className="flex flex-wrap gap-x-3">
              <Link
                to={contentLink({ kind: 'user', userId }, 'runs', entry.id)}
                className={ROW_LINK}
              >
                {entryName(entry)}
              </Link>
              <span className="text-slate-600">{historyStatusCopy(entry)}</span>
              <span className="text-slate-600">
                {new Date(entry.requested_at).toLocaleDateString()}
              </span>
              <span className="break-all text-slate-600">
                {entry.base_cv === null
                  ? CV_DELETED_NOTE
                  : (entry.base_cv.label ?? entry.base_cv.original_filename)}
              </span>
            </li>
          ))}
        </ul>
      )}
      {history.hasNextPage && (
        <button
          type="button"
          onClick={() => {
            void history.fetchNextPage();
          }}
          disabled={history.isFetchingNextPage}
          className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60"
        >
          {history.isFetchingNextPage ? LOADING_MORE_LABEL : LOAD_MORE_LABEL}
        </button>
      )}
    </ContentSection>
  );
}
