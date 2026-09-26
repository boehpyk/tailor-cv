import { useMutation, useQueryClient } from '@tanstack/react-query';

import { uploadSavedBaseCv } from '@/api/savedBaseCvs';

import { savedBaseCvsQueryKeyPrefix, uploadSavedBaseCvMutationKey } from './savedCvsKeys';

import type { SavedBaseCv } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * Upload a CV to the account (`POST /api/me/base-cvs`), then invalidate every saved-CV list — the
 * server's answer to "what does the list look like now" is the only one, so the new row is never
 * written into the cache by hand (1.1's `useUploadBaseCv` reasoning).
 *
 * The invalidation is **returned**, so the mutation stays pending until the list has been re-read:
 * "Uploading and reading your CV…" gives way to the new row in one step, never to a gap in which
 * the upload has finished and the list does not show it yet.
 *
 * An `extraction_failed` CV is a **success** here (201): it is saved, listed, and its row shows the
 * server's `failure_message` (AC-35). Pending and error are the mutation's own; the section reads
 * them. The per-account cap is **not** checked here — the 409 is rendered.
 */
export function useUploadSavedBaseCv(): UseMutationResult<SavedBaseCv, Error, File> {
  const queryClient = useQueryClient();

  return useMutation<SavedBaseCv, Error, File>({
    mutationKey: uploadSavedBaseCvMutationKey,
    mutationFn: (file) => uploadSavedBaseCv(file),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: savedBaseCvsQueryKeyPrefix }),
  });
}
