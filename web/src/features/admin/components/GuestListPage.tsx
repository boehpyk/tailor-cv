import { useEffect, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router';

import {
  COLUMN_EXPIRES,
  COLUMN_STARTED,
  FIELD_ID,
  GUESTS_CAPTION,
  GUESTS_EMPTY,
  GUESTS_ERROR,
  GUESTS_HEADING,
  GUESTS_LOADING,
  GUEST_COUNT_LABELS,
  LOADING_MORE_LABEL,
  LOAD_MORE_LABEL,
  guestUnlinkFailuresNote,
} from '../adminCopy';
import { contentNoticeFrom } from '../contentNotice';
import { padded, subjectLink } from '../contentView';
import { useGuestSessions } from '../hooks/useGuestSessions';

import { ErrorWithRetry } from './ContentPage';
import { EMPTY } from './ContentSection';

import type { AdminGuestSessionSummary } from '@/api/admin';

export interface GuestListPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

const BUTTON =
  'rounded-md bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60';
const COUNT_KEYS = Object.keys(GUEST_COUNT_LABELS) as (keyof typeof GUEST_COUNT_LABELS)[];

/**
 * `/admin/guests` (AC-32): live guest sessions, latest expiry first, *Load more* while the server
 * has more. An erase's notice arrives in `location.state` and is read once, then the history entry
 * is replaced without it — `UserPage`'s pattern.
 */
export function GuestListPage({ actorId }: GuestListPageProps): React.JSX.Element {
  const sessions = useGuestSessions(actorId);
  const location = useLocation();
  const navigate = useNavigate();
  const [notice] = useState(() => contentNoticeFrom(location.state));
  const carriesNotice = contentNoticeFrom(location.state) !== null;

  useEffect(() => {
    if (carriesNotice) {
      void navigate(
        { pathname: location.pathname, search: location.search, hash: location.hash },
        { replace: true, state: null },
      );
    }
  }, [carriesNotice, navigate, location.pathname, location.search, location.hash]);

  return (
    <section aria-labelledby="admin-guests-heading" className="mb-10 space-y-4">
      <h1 id="admin-guests-heading" className="font-display text-xl font-bold text-slate-900">
        {GUESTS_HEADING}
      </h1>
      {notice !== null && (
        <div
          role="status"
          className="rounded-lg border border-slate-200 bg-surface-sunk p-4 text-sm text-slate-800"
        >
          <p>{notice.text}</p>
          {notice.unlinkFailures > 0 && <p>{guestUnlinkFailuresNote(notice.unlinkFailures)}</p>}
        </div>
      )}
      <ListBody sessions={sessions} />
    </section>
  );
}

function ListBody({
  sessions,
}: {
  readonly sessions: ReturnType<typeof useGuestSessions>;
}): React.JSX.Element {
  if (sessions.data === undefined) {
    return sessions.error !== null ? (
      <ErrorWithRetry text={GUESTS_ERROR} query={sessions} />
    ) : (
      <p role="status" className="text-slate-600">
        {GUESTS_LOADING}
      </p>
    );
  }
  const items = sessions.data.pages.flatMap((page) => page.items);
  if (items.length === 0) return <p className={EMPTY}>{GUESTS_EMPTY}</p>;
  return (
    <>
      <GuestTable sessions={items} />
      {sessions.isFetchNextPageError && (
        <p role="alert" className="text-slate-700">
          {GUESTS_ERROR}
        </p>
      )}
      {sessions.hasNextPage && (
        <button
          type="button"
          onClick={() => {
            void sessions.fetchNextPage();
          }}
          disabled={sessions.isFetchingNextPage}
          className={BUTTON}
        >
          {sessions.isFetchingNextPage ? LOADING_MORE_LABEL : LOAD_MORE_LABEL}
        </button>
      )}
    </>
  );
}

const TH = 'py-2 pr-4 font-medium';
const TD = 'py-2 pr-4';

/** Seven columns scroll inside their own box at 360 px; the id wraps anywhere. */
function GuestTable({
  sessions,
}: {
  readonly sessions: readonly AdminGuestSessionSummary[];
}): React.JSX.Element {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{GUESTS_CAPTION}</caption>
        <thead className="border-b border-slate-200 text-slate-600">
          <tr>
            <th scope="col" className={TH}>
              {FIELD_ID}
            </th>
            <th scope="col" className={TH}>
              {COLUMN_STARTED}
            </th>
            <th scope="col" className={TH}>
              {COLUMN_EXPIRES}
            </th>
            {COUNT_KEYS.map((key) => (
              <th key={key} scope="col" className={TH}>
                {GUEST_COUNT_LABELS[key]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sessions.map((session) => (
            <tr key={session.id} className="border-b border-slate-100">
              <td className={`${TD} break-all`}>
                <Link
                  to={subjectLink({ kind: 'guest', sessionId: session.id })}
                  className="font-medium text-brand-700 underline underline-offset-2"
                >
                  {session.id}
                </Link>
              </td>
              <td className={TD}>{new Date(session.created_at).toLocaleString()}</td>
              <td className={TD}>{new Date(session.expires_at).toLocaleString()}</td>
              {COUNT_KEYS.map((key) => (
                <td key={key} className={TD}>
                  {padded(session[key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
