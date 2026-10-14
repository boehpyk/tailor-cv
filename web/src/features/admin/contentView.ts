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
