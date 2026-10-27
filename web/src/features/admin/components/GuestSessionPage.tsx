import { Fragment } from 'react';
import { Link, useParams } from 'react-router';

import { ApiError } from '@/api/client';
import { NotFoundPage } from '@/features/tailoring/components/NotFoundPage';
import { isActiveTailoringRunStatus } from '@/features/tailoring/types';

import {
  COLUMN_EXPIRES,
  COLUMN_STARTED,
  FIELD_ID,
  GUEST_COUNT_LABELS,
  GUEST_SECTION_EMPTY,
  GUEST_SECTION_HEADINGS,
  GUEST_SESSION_ERROR,
  GUEST_SESSION_HEADING,
  GUEST_SESSION_LOADING,
  UNTITLED_POSTING,
  historyStatusCopy,
} from '../adminCopy';
import { contentLink, padded } from '../contentView';
import { useGuestSession } from '../hooks/useGuestSessions';

import { ErrorWithRetry, GuestSessionGone } from './ContentPage';
import { EMPTY, ROW_LINK } from './ContentSection';
import { DeleteGuestSessionButton } from './DeleteGuestSessionButton';

import type { AdminGuestSessionDetail, AdminSubject } from '@/api/admin';

export interface GuestSessionPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

const TERM = 'text-slate-600';
const VALUE = 'font-medium text-slate-900 break-all';
const COUNT_KEYS = Object.keys(GUEST_COUNT_LABELS) as (keyof typeof GUEST_COUNT_LABELS)[];

/**
 * `/admin/guests/:sessionId` (AC-33), error first like `UserPage`: the firewall's code-less 404 →
 * `NotFoundPage`; `guest_session_not_found` → the gone sentence. Then the overview, *Delete guest
 * session*, and the three lists the one response carries; a failed background refetch keeps them.
 */
export function GuestSessionPage({ actorId }: GuestSessionPageProps): React.JSX.Element {
  // Always set under this route; '' only satisfies the type.
  const { sessionId = '' } = useParams();
  const query = useGuestSession(actorId, sessionId);
  const { data, error } = query;

  if (error instanceof ApiError && error.status === 404) {
    return error.code === 'guest_session_not_found' ? <GuestSessionGone /> : <NotFoundPage />;
  }
  if (data !== undefined) return <SessionView actorId={actorId} detail={data} />;
  if (error !== null) return <ErrorWithRetry text={GUEST_SESSION_ERROR} query={query} />;
  return (
    <p role="status" className="mb-10 text-slate-600">
      {GUEST_SESSION_LOADING}
    </p>
  );
}

function SessionView({
  actorId,
  detail,
}: {
  readonly actorId: string;
  readonly detail: AdminGuestSessionDetail;
}): React.JSX.Element {
  const { session } = detail;
  const subject: AdminSubject = { kind: 'guest', sessionId: session.id };
  return (
    <section className="mb-10 min-w-0 space-y-6">
      <h1 className="font-display text-xl font-bold text-slate-900">{GUEST_SESSION_HEADING}</h1>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
        <dt className={TERM}>{FIELD_ID}</dt>
        <dd className={VALUE}>{session.id}</dd>
        <dt className={TERM}>{COLUMN_STARTED}</dt>
        <dd className={VALUE}>{new Date(session.created_at).toLocaleString()}</dd>
        <dt className={TERM}>{COLUMN_EXPIRES}</dt>
        <dd className={VALUE}>{new Date(session.expires_at).toLocaleString()}</dd>
        {COUNT_KEYS.map((key) => (
          <Fragment key={key}>
            <dt className={TERM}>{GUEST_COUNT_LABELS[key]}</dt>
            <dd className={VALUE}>{padded(session[key])}</dd>
          </Fragment>
        ))}
      </dl>
      <DeleteGuestSessionButton
        actorId={actorId}
        session={session}
        runInProgress={detail.tailoring_runs.some((run) => isActiveTailoringRunStatus(run.status))}
      />
      <GuestSection id="admin-guest-cvs" heading={GUEST_SECTION_HEADINGS.cvs}>
        {detail.base_cvs.length === 0 ? (
          <p className={EMPTY}>{GUEST_SECTION_EMPTY.cvs}</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {detail.base_cvs.map((cv) => (
              <Row
                key={cv.id}
                to={contentLink(subject, 'cvs', cv.id)}
                text={cv.original_filename}
                at={cv.uploaded_at}
              />
            ))}
          </ul>
        )}
      </GuestSection>
      <GuestSection id="admin-guest-postings" heading={GUEST_SECTION_HEADINGS.postings}>
        {detail.job_postings.length === 0 ? (
          <p className={EMPTY}>{GUEST_SECTION_EMPTY.postings}</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {detail.job_postings.map((posting) => (
              <Row
                key={posting.id}
                to={contentLink(subject, 'postings', posting.id)}
                text={posting.title ?? UNTITLED_POSTING}
                at={posting.created_at}
              />
            ))}
          </ul>
        )}
      </GuestSection>
      <GuestSection id="admin-guest-runs" heading={GUEST_SECTION_HEADINGS.runs}>
        {detail.tailoring_runs.length === 0 ? (
          <p className={EMPTY}>{GUEST_SECTION_EMPTY.runs}</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {detail.tailoring_runs.map((run) => (
              <Row
                key={run.id}
                to={contentLink(subject, 'runs', run.id)}
                text={historyStatusCopy(run)}
                at={run.requested_at}
              />
            ))}
          </ul>
        )}
      </GuestSection>
    </section>
  );
}

function GuestSection({
  id,
  heading,
  children,
}: {
  readonly id: string;
  readonly heading: string;
  readonly children: React.ReactNode;
}): React.JSX.Element {
  return (
    <section aria-labelledby={id} className="space-y-3">
      <h2 id={id} className="font-display text-base font-bold text-slate-900">
        {heading}
      </h2>
      {children}
    </section>
  );
}

function Row({
  to,
  text,
  at,
}: {
  readonly to: string;
  readonly text: string;
  readonly at: string;
}): React.JSX.Element {
  return (
    <li className="flex flex-wrap gap-x-3">
      <Link to={to} className={ROW_LINK}>
        {text}
      </Link>
      <span className="text-slate-600">{new Date(at).toLocaleString()}</span>
    </li>
  );
}
