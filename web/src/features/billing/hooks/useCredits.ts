import { useQuery } from '@tanstack/react-query';

import { fetchCredits } from '@/api/credits';
import { isClientError, MAX_TRANSIENT_RETRIES } from '@/features/tailoring/hooks/useTailoringRun';

import { creditsQueryKey } from './billingKeys';

import type { Credits, PaymentNoticeState } from '../types';
import type { QueryClient, UseQueryResult } from '@tanstack/react-query';

/**
 * One account's credits. **Never copied into state**: the launch, the notice, the out-of-credits
 * swap and the account page all read this one entry. Reads retry (ADR-0031): a 4xx is an answer, a
 * network failure or 5xx gets `MAX_TRANSIENT_RETRIES`.
 *
 * A failed read never disables Tailor (plan §5): the server decides, and its 409 `no_credits` is
 * what shows the out-of-credits panel when this read could not.
 */
export function useCredits(userId: string): UseQueryResult<Credits> {
  return useQuery({
    queryKey: creditsQueryKey(userId),
    queryFn: ({ signal }) => fetchCredits(signal),
    retry: (failureCount, error) => !isClientError(error) && failureCount < MAX_TRANSIENT_RETRIES,
  });
}

/**
 * Write a pack click's or an opt-in's answer into the credits read, **only if that read still
 * exists** (5.1's round-3 lesson): a sign-out mid-request removed it, and it must stay removed.
 */
export function writePaymentNotice(
  queryClient: QueryClient,
  userId: string,
  answer: PaymentNoticeState,
): void {
  const queryKey = creditsQueryKey(userId);
  if (queryClient.getQueryCache().find({ queryKey, exact: true }) === undefined) {
    return;
  }
  queryClient.setQueryData<Credits>(queryKey, (old) =>
    old === undefined ? old : { ...old, payment_notice_opted_in: answer.payment_notice_opted_in },
  );
}
