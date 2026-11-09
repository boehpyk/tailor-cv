import { CREDITS_NOTICE } from '../billingCopy';
import { useCredits } from '../hooks/useCredits';

export interface CreditsNoticeProps {
  /** The signed-in user, from `AccountScope`: roots the credits key. */
  readonly userId: string;
}

/**
 * The one-time credits notice above the account's launch (AC-34): shown while `notice` is
 * `credits_introduced` or `first_credit`, nothing otherwise (and nothing while loading or failed).
 * It goes when a read says the account has been charged: the server clears `notice`.
 */
export function CreditsNotice({ userId }: CreditsNoticeProps): React.JSX.Element | null {
  const notice = useCredits(userId).data?.notice ?? null;
  if (notice === null) {
    return null;
  }
  return (
    <p className="rounded-lg border border-slate-200 bg-brand-50 px-4 py-3 text-sm text-brand-700">
      {CREDITS_NOTICE[notice]}
    </p>
  );
}
