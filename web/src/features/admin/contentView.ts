import { ApiError } from '@/api/client';

import type { AdminSubject } from '@/api/admin';

import { DOWNLOAD_FAILED, DOWNLOAD_FILE_GONE } from './adminCopy';

/** `/admin/users/:userId` or `/admin/guests/:sessionId` — the subject's own page. */
export function subjectLink(subject: AdminSubject): string {
  return subject.kind === 'user'
    ? `/admin/users/${encodeURIComponent(subject.userId)}`
    : `/admin/guests/${encodeURIComponent(subject.sessionId)}`;
}

/** `<subject page>/<kind>/:id` — every content link is built here. */
export function contentLink(
  subject: AdminSubject,
  kind: 'cvs' | 'runs' | 'postings',
  id: string,
): string {
  return `${subjectLink(subject)}/${kind}/${encodeURIComponent(id)}`;
}

/** A refused download, in words: 410 is a file that is gone; anything else, try again. */
export function downloadFailureCopy(error: Error): string {
  return error instanceof ApiError && error.status === 410 ? DOWNLOAD_FILE_GONE : DOWNLOAD_FAILED;
}

/**
 * A count in a table cell or `<dd>`, padded with spaces: adjacent cells' text otherwise runs
 * together (`…10:00 AM3456`), for copy-paste and for anything reading the row as one string.
 */
export function padded(count: number): string {
  return ` ${String(count)} `;
}
