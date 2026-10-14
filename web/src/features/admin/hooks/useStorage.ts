import { useQuery } from '@tanstack/react-query';

import { getStorage } from '@/api/admin';

import { adminKey } from './useAdminUser';
import { isClientError } from './useUserDirectory';

import type { AdminStorage } from '@/api/admin';
import type { UseQueryResult } from '@tanstack/react-query';

export function storageKey(actorId: string): readonly string[] {
  return [...adminKey(actorId), 'storage'];
}

/** The volume, measured by the server on each fetch (AC-36); *Measure again* is `refetch`. */
export function useStorage(actorId: string): UseQueryResult<AdminStorage> {
  return useQuery({
    queryKey: storageKey(actorId),
    queryFn: ({ signal }) => getStorage(signal),
    retry: (failureCount, error) => !isClientError(error) && failureCount < 1,
  });
}
