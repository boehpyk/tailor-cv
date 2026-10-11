import { useEffect, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router';

import { ApiError } from '@/api/client';

import {
  ADMIN_RETRY_LABEL,
  LOADING_MORE_LABEL,
  LOAD_MORE_LABEL,
  SEARCH_INVALID,
  USERS_EMPTY,
  USERS_ERROR,
  USERS_HEADING,
  USERS_LOADING,
  erasedNotice,
  unlinkFailuresNote,
  usersNoMatch,
} from '../adminCopy';
import { erasedNoticeFrom } from '../erasedNotice';
import { useUserDirectory } from '../hooks/useUserDirectory';

import { UserSearchForm } from './UserSearchForm';
import { UserTable } from './UserTable';

export interface UserListPageProps {
  readonly actorId: string;
}

const BUTTON =
  'rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-60';

/**
 * `/admin`: the user directory (AC-31, AC-32). The search lives in the URL (`?q=`), so a submit is
 * a navigation, back/forward restore it, and a new search is a new query key from page 1 — no
 * search state of our own and no request per keystroke. The erase notice comes from
 * `location.state` (AC-35), read **once** into component state; the history entry is then replaced
 * with the same URL and no state, so the erased email does not outlive the notice in history
 * (AC-38) — `AccountDeletedNotice`'s pattern, and that replacement is the effect's whole job.
 *
 * The heading and the form always render, so a refused search (422 `invalid_search`) keeps what
 * was typed beside its message.
 */
export function UserListPage({ actorId }: UserListPageProps): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const q = params.get('q') ?? '';
  const directory = useUserDirectory(actorId, q);
  const location = useLocation();
  const navigate = useNavigate();
  const [erased] = useState(() => erasedNoticeFrom(location.state));
  const carriesNotice = erasedNoticeFrom(location.state) !== null;

  useEffect(() => {
    if (carriesNotice) {
      void navigate(
        { pathname: location.pathname, search: location.search, hash: location.hash },
        { replace: true, state: null },
      );
    }
  }, [carriesNotice, navigate, location.pathname, location.search, location.hash]);

  return (
    <section aria-labelledby="admin-users-heading" className="mb-10 space-y-4">
      <h1 id="admin-users-heading" className="text-lg font-semibold text-slate-900">
        {USERS_HEADING}
      </h1>
      {erased !== null && (
        <div
          role="status"
          className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm text-slate-800"
        >
          <p>{erasedNotice(erased.email)}</p>
          {erased.unlinkFailures > 0 && <p>{unlinkFailuresNote(erased.unlinkFailures)}</p>}
        </div>
      )}
      <UserSearchForm
        q={q}
        onSearch={(typed) => {
          const search = typed.trim();
          setParams(search === '' ? {} : { q: search });
        }}
      />
      <DirectoryBody actorId={actorId} q={q} directory={directory} />
    </section>
  );
}

function DirectoryBody({
  actorId,
  q,
  directory,
}: {
  readonly actorId: string;
  readonly q: string;
  readonly directory: ReturnType<typeof useUserDirectory>;
}): React.JSX.Element {
  if (directory.data === undefined) {
    if (directory.error === null) {
      return (
        <p role="status" className="text-slate-600">
          {USERS_LOADING}
        </p>
      );
    }
    if (directory.error instanceof ApiError && directory.error.code === 'invalid_search') {
      return (
        <p role="alert" className="text-slate-700">
          {SEARCH_INVALID}
        </p>
      );
    }
    return (
      <div role="alert" className="space-y-3">
        <p className="text-slate-700">{USERS_ERROR}</p>
        <button
          type="button"
          onClick={() => {
            void directory.refetch();
          }}
          disabled={directory.isFetching}
          className={BUTTON}
        >
          {ADMIN_RETRY_LABEL}
        </button>
      </div>
    );
  }

  const users = directory.data.pages.flatMap((p) => p.items);
  if (users.length === 0) {
    return <p className="text-slate-600">{q === '' ? USERS_EMPTY : usersNoMatch(q)}</p>;
  }
  return (
    <>
      <UserTable users={users} actorId={actorId} />
      {directory.isFetchNextPageError && (
        <p role="alert" className="text-slate-700">
          {USERS_ERROR}
        </p>
      )}
      {directory.hasNextPage && (
        <button
          type="button"
          onClick={() => {
            void directory.fetchNextPage();
          }}
          disabled={directory.isFetchingNextPage}
          className={BUTTON}
        >
          {directory.isFetchingNextPage ? LOADING_MORE_LABEL : LOAD_MORE_LABEL}
        </button>
      )}
    </>
  );
}
