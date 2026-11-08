export interface CreditsLineProps {
  /** The signed-in user, from `AccountScope`: roots the credits key. */
  readonly userId: string;
}

/**
 * The line beside an account's Tailor button (AC-33): loading *Checking your credits…*, error
 * *Couldn't load your credits.* (Tailor still enabled in both: the server decides), success
 * *Uses 1 credit · You have N*. Reads `useCredits(userId)`.
 *
 * SKELETON (T32): renders a placeholder. T34 builds it against T33's state tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- SKELETON (T32): T34 reads the props.
export function CreditsLine(_props: CreditsLineProps): React.JSX.Element {
  return <div data-skeleton="CreditsLine" />;
}
