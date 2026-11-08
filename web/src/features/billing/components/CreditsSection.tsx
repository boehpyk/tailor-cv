export interface CreditsSectionProps {
  /** The signed-in user: roots the credits key. */
  readonly userId: string;
}

/**
 * The `/account` section (plan §5): loading, error with Retry, the balance, the entries (empty:
 * *No credit activity yet.*) and the `PackOffer`. Reads `useCredits(userId)`.
 *
 * SKELETON (T32): renders a placeholder. T34 builds it against T33's state tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- SKELETON (T32): T34 reads the props.
export function CreditsSection(_props: CreditsSectionProps): React.JSX.Element {
  return <div data-skeleton="CreditsSection" />;
}
