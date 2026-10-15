import { Link } from 'react-router';

import { CONTENT_EMPTY, CONTENT_ERROR, CONTENT_HEADINGS, CONTENT_LOADING } from '../adminCopy';
import { contentLink } from '../contentView';
import { useAdminCvs } from '../hooks/useAccountContent';

import { ContentSection, EMPTY, ROW_LINK } from './ContentSection';

export interface SavedCvListProps {
  readonly actorId: string;
  /** The account whose content this lists. */
  readonly userId: string;
}

/**
 * The account's saved CVs (AC-26): loading / error + Retry / *No saved CVs.* / one row per CV,
 * its label (else its filename) linking to the CV's page.
 */
export function SavedCvList({ actorId, userId }: SavedCvListProps): React.JSX.Element {
  const cvs = useAdminCvs(actorId, userId);
  const items = cvs.data?.items ?? [];
  return (
    <ContentSection
      id="admin-content-cvs"
      heading={CONTENT_HEADINGS.cvs}
      query={cvs}
      loadingText={CONTENT_LOADING.cvs}
      errorText={CONTENT_ERROR.cvs}
    >
      {items.length === 0 ? (
        <p className={EMPTY}>{CONTENT_EMPTY.cvs}</p>
      ) : (
        <ul className="space-y-2 text-sm">
          {items.map((cv) => (
            <li key={cv.id} className="flex flex-wrap gap-x-3">
              <Link to={contentLink({ kind: 'user', userId }, 'cvs', cv.id)} className={ROW_LINK}>
                {cv.label ?? cv.original_filename}
              </Link>
              {cv.label !== null && (
                <span className="break-all text-slate-600">{cv.original_filename}</span>
              )}
              <span className="text-slate-600">
                {new Date(cv.uploaded_at).toLocaleDateString()}
              </span>
            </li>
          ))}
        </ul>
      )}
    </ContentSection>
  );
}
