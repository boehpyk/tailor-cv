import { Link, useParams } from 'react-router';

import { isActiveTailoringRunStatus } from '@/features/tailoring/types';

import {
  CV_DELETED_NOTE,
  DOCUMENT_HEADINGS,
  POSTING_MISSING_NOTE,
  RUN_CV_UNAVAILABLE,
  RUN_ERROR,
  RUN_FIELD_CV,
  RUN_FIELD_POSTING,
  RUN_FIELD_REQUESTED,
  RUN_FIELD_STATUS,
  RUN_GONE,
  RUN_HEADING,
  RUN_REFRESH_LABEL,
  RUN_STILL_RUNNING,
  UNTITLED_POSTING,
  historyStatusCopy,
} from '../adminCopy';
import { contentLink } from '../contentView';
import { useAdminCvs, useAdminPosting, useAdminRun } from '../hooks/useAccountContent';

import { BodyText } from './BodyText';
import { BackToAccount, ContentPage } from './ContentPage';
import { DeleteContentButton } from './DeleteContentButton';
import { DocumentDownloads } from './DocumentDownloads';
import { RunExports } from './RunExports';

import type { TailoredDocumentKind, TailoringRun } from '@/features/tailoring/types';

export interface AdminRunPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-32). */
  readonly actorId: string;
}

const TERM = 'text-slate-600';
const VALUE = 'font-medium text-slate-900 break-words';

/**
 * `/admin/users/:userId/runs/:runId` (AC-28): the run's status as its history row words it, its
 * posting (a link to the posting page) and CV (label, or *CV deleted*), both documents as text
 * with their inline downloads (succeeded only), the exports, and *Delete history entry*. A queued
 * or running run says so and offers *Refresh* — no polling (OQ-12).
 */
export function AdminRunPage({ actorId }: AdminRunPageProps): React.JSX.Element {
  // Always set under this route; '' only satisfies the type.
  const { userId = '', runId = '' } = useParams();
  const run = useAdminRun(actorId, userId, runId);
  return (
    <ContentPage
      query={run}
      userId={userId}
      goneCode="tailoring_run_not_found"
      goneText={RUN_GONE}
      errorText={RUN_ERROR}
    >
      {(data) => (
        <RunView
          actorId={actorId}
          userId={userId}
          run={data}
          refreshing={run.isFetching}
          onRefresh={() => {
            void run.refetch();
          }}
        />
      )}
    </ContentPage>
  );
}

interface RunViewProps {
  readonly actorId: string;
  readonly userId: string;
  readonly run: TailoringRun;
  readonly refreshing: boolean;
  readonly onRefresh: () => void;
}

function RunView({ actorId, userId, run, refreshing, onRefresh }: RunViewProps): React.JSX.Element {
  // The posting by id (it may be older than the 20 the list holds); the CV's label from the list,
  // which carries no text — fetching a 250 000-character CV for its label would be waste.
  const posting = useAdminPosting(actorId, userId, run.job_posting_id);
  const cvs = useAdminCvs(actorId, userId);
  const cv = cvs.data?.items.find((item) => item.id === run.base_cv_id);
  const postingTitle =
    posting.data !== undefined ? (posting.data.title ?? UNTITLED_POSTING) : POSTING_MISSING_NOTE;

  return (
    <>
      <BackToAccount userId={userId} />
      <h1 className="text-lg font-semibold text-slate-900">{RUN_HEADING}</h1>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
        <dt className={TERM}>{RUN_FIELD_STATUS}</dt>
        <dd className={VALUE}>{historyStatusCopy(run)}</dd>
        <dt className={TERM}>{RUN_FIELD_POSTING}</dt>
        <dd className={VALUE}>
          {posting.data !== undefined ? (
            <Link
              to={contentLink(userId, 'postings', posting.data.id)}
              className="underline underline-offset-2"
            >
              {postingTitle}
            </Link>
          ) : posting.error !== null ? (
            POSTING_MISSING_NOTE
          ) : null}
        </dd>
        <dt className={TERM}>{RUN_FIELD_CV}</dt>
        <dd className={VALUE}>
          {cvs.data !== undefined
            ? cv === undefined
              ? CV_DELETED_NOTE
              : (cv.label ?? cv.original_filename)
            : cvs.error !== null
              ? RUN_CV_UNAVAILABLE
              : null}
        </dd>
        <dt className={TERM}>{RUN_FIELD_REQUESTED}</dt>
        <dd className={VALUE}>{new Date(run.requested_at).toLocaleString()}</dd>
      </dl>
      {isActiveTailoringRunStatus(run.status) && (
        <div className="space-y-2">
          <p className="text-sm text-slate-700">{RUN_STILL_RUNNING}</p>
          <button
            type="button"
            onClick={onRefresh}
            disabled={refreshing}
            className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-900 disabled:opacity-60"
          >
            {RUN_REFRESH_LABEL}
          </button>
        </div>
      )}
      {run.status === 'succeeded' && (
        <>
          <DocumentSection userId={userId} runId={run.id} document="cv" text={run.tailored_cv} />
          <DocumentSection
            userId={userId}
            runId={run.id}
            document="cover_letter"
            text={run.cover_letter}
          />
        </>
      )}
      <RunExports actorId={actorId} userId={userId} runId={run.id} />
      {(posting.data !== undefined || posting.error !== null) && (
        <DeleteContentButton
          actorId={actorId}
          userId={userId}
          target={{ kind: 'history', id: run.id, postingTitle, requestedAt: run.requested_at }}
        />
      )}
    </>
  );
}

interface DocumentSectionProps {
  readonly userId: string;
  readonly runId: string;
  readonly document: TailoredDocumentKind;
  readonly text: string | null;
}

function DocumentSection({
  userId,
  runId,
  document,
  text,
}: DocumentSectionProps): React.JSX.Element {
  const id = `admin-run-${document}`;
  return (
    <section aria-labelledby={id} className="space-y-2">
      <h2 id={id} className="text-base font-semibold text-slate-900">
        {DOCUMENT_HEADINGS[document]}
      </h2>
      {text !== null && <BodyText text={text} />}
      <DocumentDownloads userId={userId} runId={runId} document={document} />
    </section>
  );
}
