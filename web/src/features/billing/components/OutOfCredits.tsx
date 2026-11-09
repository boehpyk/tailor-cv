import { OUT_OF_CREDITS_HEADING, OUT_OF_CREDITS_KEEP, PACKS_HEADING } from '../billingCopy';
import { useCredits } from '../hooks/useCredits';

import { PackOffer } from './PackOffer';

export interface OutOfCreditsProps {
  /** The signed-in user, from `AccountScope`: roots the credits key and the pack click's. */
  readonly userId: string;
}

/**
 * In place of `TailorLaunch` when `out_of_credits`, or after a 409 `no_credits` (AC-33): static
 * copy (everything made stays here) and the `PackOffer` with the server's packs. A failed credits
 * read still says it (the 409 is the authority), with no pack buttons: their prices are the read's.
 */
export function OutOfCredits({ userId }: OutOfCreditsProps): React.JSX.Element {
  const packs = useCredits(userId).data?.packs;
  return (
    <div className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
      <h3 className="font-display text-base font-semibold text-slate-900">
        {OUT_OF_CREDITS_HEADING}
      </h3>
      <p className="text-sm text-slate-700">{OUT_OF_CREDITS_KEEP}</p>
      {packs !== undefined && packs.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-sm font-semibold text-slate-900">{PACKS_HEADING}</h4>
          <PackOffer userId={userId} packs={packs} />
        </div>
      )}
    </div>
  );
}
