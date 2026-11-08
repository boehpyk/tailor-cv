import type { PackTerms } from '../types';

export interface PackDialogProps {
  /** The signed-in user: roots the pack click's and the opt-in's keys and the credits read. */
  readonly userId: string;
  /** The pack that was clicked. */
  readonly pack: PackTerms;
  /** Closes the dialog; the opener owns whether it is open. */
  readonly onClose: () => void;
}

/**
 * Opened by a pack click (AC-35): the click's pending, error (*That didn't go through. Nothing was
 * charged.*) and success (payments opening soon, nothing charged), and on success the opt-in
 * checkbox naming the account's address (`useCurrentUser`), with its own pending and error
 * (`usePaymentNotice`), its checked state from the credits read.
 *
 * SKELETON (T32): renders a placeholder. T34 builds it against T33's state tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- SKELETON (T32): T34 reads the props.
export function PackDialog(_props: PackDialogProps): React.JSX.Element {
  return <div data-skeleton="PackDialog" />;
}
