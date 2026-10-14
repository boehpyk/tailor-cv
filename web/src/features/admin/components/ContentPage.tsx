import { Link } from 'react-router';

import { ApiError } from '@/api/client';
import { NotFoundPage } from '@/features/tailoring/components/NotFoundPage';

import {
  ADMIN_RETRY_LABEL,
  BACK_TO_ACCOUNT,
  BACK_TO_USERS,
  USER_GONE,
  USER_LOADING,
} from '../adminCopy';
import { subjectLink } from '../contentView';

import type { AdminSubject } from '@/api/admin';
import type { UseQueryResult } from '@tanstack/react-query';

export interface ContentPageProps<T> {
  readonly query: UseQueryResult<T>;
  readonly subject: AdminSubject;
  /** The item kind's own 404 code, and what the page says for it. */
  readonly goneCode: string;
  readonly goneText: string;
  readonly errorText: string;
  readonly children: (data: T) => React.ReactNode;
}

const LINK = 'font-medium text-slate-900 underline underline-offset-2';

/**
 * The views every admin read page shares (AC-27), error first like `UserPage`: the firewall's
 * code-less 404 → the app's `NotFoundPage`; `user_not_found` → the account is gone, link to
 * `/admin`; the kind's 404 → the item is gone, link to the account. Those outrank cached data; any
 * other error does not, so a failed background refetch never hides the page. Then the page, an
 * alert with Retry, or loading.
 */
export function ContentPage<T>({
  query,
  subject,
  goneCode,
  goneText,
  errorText,
  children,
}: ContentPageProps<T>): React.JSX.Element {
  const { error, data } = query;
  if (error instanceof ApiError && error.status === 404) {
    if (error.code === 'user_not_found') {
      return (
        <section className="mb-10 space-y-3">
          <p className="text-slate-700">{USER_GONE}</p>
          <Link to="/admin" className={LINK}>
            {BACK_TO_USERS}
          </Link>
        </section>
      );
    }
    if (error.code === goneCode) {
      return (
        <section className="mb-10 space-y-3">
          <p className="text-slate-700">{goneText}</p>
          <BackToAccount subject={subject} />
        </section>
      );
    }
    return <NotFoundPage />;
  }
  if (data !== undefined) {
    return <article className="mb-10 min-w-0 space-y-6">{children(data)}</article>;
  }
  if (error !== null) {
    return <ErrorWithRetry text={errorText} query={query} />;
  }
  return (
    <p role="status" className="mb-10 text-slate-600">
      {USER_LOADING}
    </p>
  );
}

/** A failed read: `role="alert"`, visibly not loading, with Retry (disabled while refetching). */
export function ErrorWithRetry({
  text,
  query,
}: {
  readonly text: string;
  readonly query: Pick<UseQueryResult, 'refetch' | 'isFetching'>;
}): React.JSX.Element {
  return (
    <div role="alert" className="space-y-3">
      <p className="text-slate-700">{text}</p>
      <button
        type="button"
        onClick={() => {
          void query.refetch();
        }}
        disabled={query.isFetching}
        className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-60"
      >
        {ADMIN_RETRY_LABEL}
      </button>
    </div>
  );
}

/** A link back to the subject a read page belongs to. */
export function BackToAccount({ subject }: { readonly subject: AdminSubject }): React.JSX.Element {
  return (
    <Link to={subjectLink(subject)} className={LINK}>
      {BACK_TO_ACCOUNT}
    </Link>
  );
}
