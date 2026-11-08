import { useState } from 'react';
import { useMutationState, useQueryClient } from '@tanstack/react-query';

import { PACK_PENDING, packLabel } from '../billingCopy';
import { packClickMutationKey, usePackClick } from '../hooks/usePackClick';

import { PackDialog } from './PackDialog';

import type { PackId, PackTerms } from '../types';

export interface PackOfferProps {
  /** The signed-in user: roots the pack click's mutation key. */
  readonly userId: string;
  /** The server's packs (`Credits.packs`), in its order. */
  readonly packs: readonly PackTerms[];
}

/**
 * One button per pack (AC-25). A click sends `usePackClick` (guarded per pack by the client's
 * synchronous `isMutating`; `isPending` lags a tick) and opens `PackDialog` for that pack. Pending
 * is per button, read from the mutation cache: one observer only follows its latest `mutate`.
 *
 * Which dialog is open is UI state, so it is `useState`; the click's outcome is the cache's.
 */
export function PackOffer({ userId, packs }: PackOfferProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const packClick = usePackClick(userId);
  const mutationKey = packClickMutationKey(userId);
  const [open, setOpen] = useState<PackId | null>(null);
  const pending = useMutationState({
    filters: { mutationKey, status: 'pending' },
    select: (m) => m.state.variables as PackId | undefined,
  });
  const openPack = packs.find((p) => p.id === open);

  function choose(pack: PackId): void {
    setOpen(pack);
    const inFlight = queryClient.isMutating({
      mutationKey,
      predicate: (m) => m.state.variables === pack,
    });
    if (inFlight === 0) {
      packClick.mutate(pack);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-3">
        {packs.map((pack) => {
          const busy = pending.includes(pack.id);
          return (
            <button
              key={pack.id}
              type="button"
              onClick={() => {
                choose(pack.id);
              }}
              disabled={busy}
              className="min-h-11 rounded-lg border border-brand-600 bg-white px-4 text-sm font-semibold text-brand-700 hover:bg-brand-50 disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 max-sm:w-full sm:min-h-9"
            >
              {busy ? PACK_PENDING : packLabel(pack.credits, pack.price_cents)}
            </button>
          );
        })}
      </div>
      {openPack !== undefined && (
        <PackDialog
          key={openPack.id}
          userId={userId}
          pack={openPack}
          onClose={() => {
            setOpen(null);
          }}
        />
      )}
    </div>
  );
}
