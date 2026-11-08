export interface CreditsNoticeProps {
  /** The signed-in user, from `AccountScope`: roots the credits key. */
  readonly userId: string;
}

/**
 * The one-time credits notice above the account's launch (AC-34): shown while `notice` is
 * `credits_introduced` or `first_credit`, nothing otherwise (and nothing while loading or failed).
 * Reads `useCredits(userId)`.
 *
 * SKELETON (T32): renders a placeholder. T34 builds it against T33's state tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- SKELETON (T32): T34 reads the props.
export function CreditsNotice(_props: CreditsNoticeProps): React.JSX.Element {
  return <div data-skeleton="CreditsNotice" />;
}
