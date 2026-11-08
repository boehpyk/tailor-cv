export interface OutOfCreditsProps {
  /** The signed-in user, from `AccountScope`: roots the credits key and the pack click's. */
  readonly userId: string;
}

/**
 * In place of `TailorLaunch` when `out_of_credits`, or after a 409 `no_credits` (AC-33): static
 * copy (everything made stays here) and the `PackOffer` with the packs from `useCredits(userId)`.
 *
 * SKELETON (T32): renders a placeholder. T34 builds it against T33's state tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- SKELETON (T32): T34 reads the props.
export function OutOfCredits(_props: OutOfCreditsProps): React.JSX.Element {
  return <div data-skeleton="OutOfCredits" />;
}
