import { CREDITS_LOAD_ERROR, CREDITS_LOADING, creditsLine } from '../billingCopy';
import { useCredits } from '../hooks/useCredits';

export interface CreditsLineProps {
  /** The signed-in user, from `AccountScope`: roots the credits key. */
  readonly userId: string;
}

/**
 * The line beside an account's Tailor button (AC-33): loading *Checking your credits…*, error
 * *Couldn't load your credits.* (Tailor still enabled in both: the server decides), success
 * *Uses 1 credit · You have N*. Reads `useCredits(userId)`; disables nothing.
 */
export function CreditsLine({ userId }: CreditsLineProps): React.JSX.Element {
  const credits = useCredits(userId);
  const text =
    credits.data !== undefined
      ? creditsLine(credits.data.balance)
      : credits.isError
        ? CREDITS_LOAD_ERROR
        : CREDITS_LOADING;
  return <p className="text-sm text-slate-600">{text}</p>;
}
