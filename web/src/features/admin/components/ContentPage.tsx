import { Link } from 'react-router';

import { ApiError } from '@/api/client';
import { NotFoundPage } from '@/features/tailoring/components/NotFoundPage';

import {
  ADMIN_RETRY_LABEL,
  BACK_TO_ACCOUNT,
  BACK_TO_GUESTS,
  BACK_TO_GUEST_SESSION,
  BACK_TO_USERS,
  GUEST_SESSION_GONE,
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

const LINK = 'font-medium text-brand-700 underline underline-offset-2';

/**
 * The views every admin read page shares (AC-27), error first like `UserPage`: the firewall's
 * code-less 404 → the app's `NotFoundPage`; `user_not_found` → the account is gone, link to
 * `/admin`; `guest_session_not_found` → the session is gone, link to `/admin/guests` (slice 4.4,
 * AC-35); the kind's 404 → the item is gone, link to the account. Those outrank cached data; any
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
    if (error.code === 'guest_session_not_found') return <GuestSessionGone />;
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
        className="rounded-md bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60"
      >
        {ADMIN_RETRY_LABEL}
      </button>
    </div>
  );
}

/** A link back to the subject a read page belongs to: the account, or the guest session. */
export function BackToAccount({ subject }: { readonly subject: AdminSubject }): React.JSX.Element {
  return (
    <Link to={subjectLink(subject)} className={LINK}>
      {subject.kind === 'user' ? BACK_TO_ACCOUNT : BACK_TO_GUEST_SESSION}
    </Link>
  );
}

/** 404 `guest_session_not_found` (AC-33, AC-35): expired, claimed or deleted — say so, link back. */
export function GuestSessionGone(): React.JSX.Element {
  return (
    <section className="mb-10 space-y-3">
      <p className="text-slate-700">{GUEST_SESSION_GONE}</p>
      <Link to="/admin/guests" className={LINK}>
        {BACK_TO_GUESTS}
      </Link>
    </section>
  );
}
