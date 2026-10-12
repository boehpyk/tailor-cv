import { useParams } from 'react-router';

import {
  CV_ERROR,
  CV_FIELD_CHARACTERS,
  CV_FIELD_FILENAME,
  CV_FIELD_SIZE,
  CV_FIELD_TYPE,
  CV_FIELD_UPLOADED,
  CV_GONE,
  CV_TEXT_HEADING,
  cvExtractionFailed,
} from '../adminCopy';
import { useAdminCv } from '../hooks/useAccountContent';

import { BodyText } from './BodyText';
import { BackToAccount, ContentPage } from './ContentPage';
import { DeleteContentButton } from './DeleteContentButton';

export interface AdminCvPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-32). */
  readonly actorId: string;
}

const TERM = 'text-slate-600';
const VALUE = 'font-medium text-slate-900 break-all';

/**
 * `/admin/users/:userId/cvs/:baseCvId` (AC-27): the label (else filename) as `<h1>`, the fields
 * as a `<dl>`, then the extracted text through `BodyText` — or the extraction failure's own
 * message — and *Delete CV*.
 */
export function AdminCvPage({ actorId }: AdminCvPageProps): React.JSX.Element {
  // Always set under this route; '' only satisfies the type.
  const { userId = '', baseCvId = '' } = useParams();
  const cv = useAdminCv(actorId, userId, baseCvId);
  return (
    <ContentPage
      query={cv}
      userId={userId}
      goneCode="base_cv_not_found"
      goneText={CV_GONE}
      errorText={CV_ERROR}
    >
      {(data) => {
        const name = data.label ?? data.original_filename;
        return (
          <>
            <BackToAccount userId={userId} />
            <h1 className="text-lg font-semibold break-words text-slate-900">{name}</h1>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
              <dt className={TERM}>{CV_FIELD_FILENAME}</dt>
              <dd className={VALUE}>{data.original_filename}</dd>
              <dt className={TERM}>{CV_FIELD_TYPE}</dt>
              <dd className={VALUE}>{data.content_type}</dd>
              <dt className={TERM}>{CV_FIELD_SIZE}</dt>
              <dd className={VALUE}>{`${data.size_bytes.toLocaleString()} bytes`}</dd>
              <dt className={TERM}>{CV_FIELD_UPLOADED}</dt>
              <dd className={VALUE}>{new Date(data.uploaded_at).toLocaleString()}</dd>
              <dt className={TERM}>{CV_FIELD_CHARACTERS}</dt>
              <dd className={VALUE}>{data.character_count?.toLocaleString() ?? '—'}</dd>
            </dl>
            <section aria-labelledby="admin-cv-text" className="space-y-2">
              <h2 id="admin-cv-text" className="text-base font-semibold text-slate-900">
                {CV_TEXT_HEADING}
              </h2>
              {data.extracted_text !== null ? (
                <BodyText text={data.extracted_text} />
              ) : (
                <p className="text-sm text-slate-700">
                  {cvExtractionFailed(data.failure_message ?? '')}
                </p>
              )}
            </section>
            <DeleteContentButton
              actorId={actorId}
              userId={userId}
              target={{ kind: 'cv', id: data.id, name }}
            />
          </>
        );
      }}
    </ContentPage>
  );
}
