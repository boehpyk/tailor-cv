import { Link } from 'react-router';

import {
  GUEST_OFFER_HEADING,
  GUEST_OFFER_RUN,
  GUEST_OFFER_SIGN_IN,
  GUEST_OFFER_SIGN_UP,
  GUEST_OFFER_WORKSPACE,
} from '../billingCopy';

export interface GuestTailorOfferProps {
  /**
   * `'workspace'`: below the guest's fit score. `'run'`: on a guest run page, where the export bar
   * was (AC-36). Copy only; both link to `/register`.
   */
  readonly placement: 'workspace' | 'run';
}

/** The guest's offer (AC-36): create a free account, and the first tailoring is free. Static. */
export function GuestTailorOffer({ placement }: GuestTailorOfferProps): React.JSX.Element {
  return (
    <div className="space-y-3 rounded-lg border border-slate-200 bg-brand-50 p-4">
      <h3 className="font-display text-base font-semibold text-slate-900">{GUEST_OFFER_HEADING}</h3>
      <p className="text-sm text-slate-700">
        {placement === 'workspace' ? GUEST_OFFER_WORKSPACE : GUEST_OFFER_RUN}
      </p>
      <div className="flex flex-wrap items-center gap-4">
        <Link
          to="/register"
          className="inline-flex min-h-11 items-center rounded-lg bg-brand-600 px-4 text-sm font-semibold text-white hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 max-sm:w-full max-sm:justify-center sm:min-h-9"
        >
          {GUEST_OFFER_SIGN_UP}
        </Link>
        <Link
          to="/login"
          className="inline-flex min-h-11 items-center text-sm font-semibold text-brand-700 underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9"
        >
          {GUEST_OFFER_SIGN_IN}
        </Link>
      </div>
    </div>
  );
}
