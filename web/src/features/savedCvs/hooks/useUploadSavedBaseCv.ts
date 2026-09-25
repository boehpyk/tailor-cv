import { useMutation } from '@tanstack/react-query';

import { uploadSavedBaseCvMutationKey } from './savedCvsKeys';

import type { SavedBaseCv } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * Upload a CV to the account (`POST /api/me/base-cvs`), then invalidate every saved-CV list — the
 * server's answer to "what does the list look like now" is the only one, so the new row is never
 * written into the cache by hand (1.1's `useUploadBaseCv` reasoning).
 *
 * An `extraction_failed` CV is a **success** here (201): it is saved, listed, and its row shows the
 * server's `failure_message` (AC-35). Pending and error are the mutation's own; the section reads
 * them. The per-account cap is **not** checked here — the 409 is rendered.
 *
 * SKELETON (T25): the real key; the mutation rejects without a request. GREEN is T27.
 */
export function useUploadSavedBaseCv(): UseMutationResult<SavedBaseCv, Error, File> {
  return useMutation<SavedBaseCv, Error, File>({
    mutationKey: uploadSavedBaseCvMutationKey,
    mutationFn: () => Promise.reject(new Error('useUploadSavedBaseCv: not implemented (T27)')),
  });
}
