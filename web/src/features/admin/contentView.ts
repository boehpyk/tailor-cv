import { ApiError } from '@/api/client';

import { DOWNLOAD_FAILED, DOWNLOAD_FILE_GONE } from './adminCopy';

/** `/admin/users/:userId/<kind>/:id` — every content link is built here. */
export function contentLink(userId: string, kind: 'cvs' | 'runs' | 'postings', id: string): string {
  return `/admin/users/${encodeURIComponent(userId)}/${kind}/${encodeURIComponent(id)}`;
}

/** A refused download, in words: 410 is a file that is gone; anything else, try again. */
export function downloadFailureCopy(error: Error): string {
  return error instanceof ApiError && error.status === 410 ? DOWNLOAD_FILE_GONE : DOWNLOAD_FAILED;
}
