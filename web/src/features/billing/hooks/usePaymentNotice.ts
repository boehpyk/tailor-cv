import { useMutation, useQueryClient } from '@tanstack/react-query';

import { putPaymentNotice } from '@/api/credits';

import { creditsQueryKey } from './billingKeys';
import { writePaymentNotice } from './useCredits';

import type { PaymentNoticeState } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/** The opt-in's mutation key; a double-toggle guard reads it with `isMutating`. */
export function paymentNoticeMutationKey(userId: string): readonly unknown[] {
  return [...creditsQueryKey(userId), 'paymentNotice'];
}

/**
 * Opt in to (`true`) or out of (`false`) one plain-text email when payments open (AC-26). No
 * `retry` (ADR-0031). On success the answer lands in the credits read if it exists, else nowhere.
 */
export function usePaymentNotice(
  userId: string,
): UseMutationResult<PaymentNoticeState, Error, boolean> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: paymentNoticeMutationKey(userId),
    mutationFn: (optedIn: boolean) => putPaymentNotice(optedIn),
    onSuccess: (answer) => {
      writePaymentNotice(queryClient, userId, answer);
    },
  });
}
