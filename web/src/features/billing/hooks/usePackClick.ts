import { useMutation, useQueryClient } from '@tanstack/react-query';

import { postPackClick } from '@/api/credits';

import { creditsQueryKey } from './billingKeys';
import { writePaymentNotice } from './useCredits';

import type { PackId, PaymentNoticeState } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * The pack click's mutation key. Variables are the `PackId`, so a per-button pending state and a
 * same-tick double-click guard filter the mutation cache by it (`isMutating` is synchronous;
 * `isPending` lags a tick).
 */
export function packClickMutationKey(userId: string): readonly unknown[] {
  return [...creditsQueryKey(userId), 'packClick'];
}

/**
 * An account chooses a pack (AC-25). **Charges nothing.** No `retry`: a write holds (ADR-0031). On
 * success the answer's opt-in state lands in the credits read if it exists, else nowhere.
 */
export function usePackClick(userId: string): UseMutationResult<PaymentNoticeState, Error, PackId> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: packClickMutationKey(userId),
    mutationFn: (pack: PackId) => postPackClick(pack),
    onSuccess: (answer) => {
      writePaymentNotice(queryClient, userId, answer);
    },
  });
}
