import { useParams } from 'react-router';

import {
  POSTING_ERROR,
  POSTING_FIELD_CAPTURED,
  POSTING_FIELD_SOURCE,
  POSTING_FIELD_URL,
  POSTING_GONE,
  POSTING_SOURCE_LABELS,
  POSTING_TEXT_HEADING,
  UNTITLED_POSTING,
} from '../adminCopy';
import { useAdminPosting } from '../hooks/useAccountContent';
import { useAdminSubject } from '../hooks/useAdminSubject';

import { BodyText } from './BodyText';
import { BackToAccount, ContentPage } from './ContentPage';

export interface AdminPostingPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-32). */
  readonly actorId: string;
}

const TERM = 'text-slate-600';
const VALUE = 'font-medium text-slate-900 break-all';

/**
 * `/admin/users/:userId/postings/:postingId` (AC-25): the title as `<h1>`, the source, the source
 * URL **as text, never a link** (OQ-13: an admin does not visit a stranger's URL by accident), and
 * the text through `BodyText`. No delete: a posting goes with its history entry.
 */
export function AdminPostingPage({ actorId }: AdminPostingPageProps): React.JSX.Element {
  // Always set under this route; '' only satisfies the type.
  const { postingId = '' } = useParams();
  const subject = useAdminSubject();
  const posting = useAdminPosting(actorId, subject, postingId);
  return (
    <ContentPage
      query={posting}
      subject={subject}
      goneCode="job_posting_not_found"
      goneText={POSTING_GONE}
      errorText={POSTING_ERROR}
    >
      {(data) => (
        <>
          <BackToAccount subject={subject} />
          <h1 className="font-display text-xl font-bold break-words text-slate-900">
            {data.title ?? UNTITLED_POSTING}
          </h1>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
            <dt className={TERM}>{POSTING_FIELD_SOURCE}</dt>
            <dd className={VALUE}>{POSTING_SOURCE_LABELS[data.source]}</dd>
            {data.source_url !== null && (
              <>
                <dt className={TERM}>{POSTING_FIELD_URL}</dt>
                <dd className={VALUE}>{data.source_url}</dd>
              </>
            )}
            <dt className={TERM}>{POSTING_FIELD_CAPTURED}</dt>
            <dd className={VALUE}>{new Date(data.created_at).toLocaleString()}</dd>
          </dl>
          <section aria-labelledby="admin-posting-text" className="space-y-2">
            <h2 id="admin-posting-text" className="font-display text-base font-bold text-slate-900">
              {POSTING_TEXT_HEADING}
            </h2>
            <BodyText text={data.text} />
          </section>
        </>
      )}
    </ContentPage>
  );
}
