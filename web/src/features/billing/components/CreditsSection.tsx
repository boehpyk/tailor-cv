import {
  CREDITS_HEADING,
  CREDITS_LOAD_ERROR,
  CREDITS_LOADING,
  ENTRIES_HEADING,
  ENTRY_LABELS,
  NO_ENTRIES,
  PACKS_HEADING,
  RETRY,
  balanceLine,
  signedCredits,
} from '../billingCopy';
import { useCredits } from '../hooks/useCredits';

import { PackOffer } from './PackOffer';

export interface CreditsSectionProps {
  /** The signed-in user: roots the credits key. */
  readonly userId: string;
}

/**
 * The `/account` section (plan §5): loading, error with Retry, the balance, the ledger (empty:
 * *No credit activity yet.*) and the `PackOffer`. Reads `useCredits(userId)`; the balance is the
 * server's, never a sum of the entries (they stop at 100).
 */
export function CreditsSection({ userId }: CreditsSectionProps): React.JSX.Element {
  const credits = useCredits(userId);
  const { data } = credits;
  return (
    <section aria-labelledby="credits-heading" className="space-y-4">
      <h2 id="credits-heading" className="font-display text-xl font-bold text-slate-900">
        {CREDITS_HEADING}
      </h2>
      {data === undefined ? (
        credits.isError ? (
          <div role="alert" className="space-y-2">
            <p className="text-sm text-slate-700">{CREDITS_LOAD_ERROR}</p>
            <button
              type="button"
              onClick={() => void credits.refetch()}
              disabled={credits.isFetching}
              className="min-h-11 rounded-lg border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-900 disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9"
            >
              {RETRY}
            </button>
          </div>
        ) : (
          <p role="status" className="text-sm text-slate-600">
            {CREDITS_LOADING}
          </p>
        )
      ) : (
        <>
          <p className="text-base font-semibold text-slate-900">{balanceLine(data.balance)}</p>
          <div className="space-y-2">
            <h3 className="text-sm font-semibold text-slate-900">{ENTRIES_HEADING}</h3>
            {data.entries.length === 0 ? (
              <p className="text-sm text-slate-600">{NO_ENTRIES}</p>
            ) : (
              <ul className="divide-y divide-slate-100 text-sm">
                {data.entries.map((entry, i) => (
                  // Entries carry no id; newest first and never reordered, so position is stable.
                  <li
                    key={`${entry.at}-${entry.kind}-${String(i)}`}
                    className="flex flex-wrap justify-between gap-x-4 py-2"
                  >
                    <span className="text-slate-700">{ENTRY_LABELS[entry.kind]}</span>
                    <span className="flex gap-4">
                      <span className="text-slate-600">
                        {new Date(entry.at).toLocaleDateString(undefined, { dateStyle: 'medium' })}
                      </span>
                      <span className="font-semibold tabular-nums text-slate-900">
                        {signedCredits(entry.credits)}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {data.packs.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-semibold text-slate-900">{PACKS_HEADING}</h3>
              <PackOffer userId={userId} packs={data.packs} />
            </div>
          )}
        </>
      )}
    </section>
  );
}
