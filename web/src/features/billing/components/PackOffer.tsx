import type { PackTerms } from '../types';

export interface PackOfferProps {
  /** The signed-in user: roots the pack click's mutation key. */
  readonly userId: string;
  /** The server's packs (`Credits.packs`), in its order. */
  readonly packs: readonly PackTerms[];
}

/**
 * One button per pack (AC-25). A click sends `usePackClick` (guarded per pack by `isMutating`)
 * and opens `PackDialog` for that pack; the pending state is per button.
 *
 * SKELETON (T32): renders a placeholder. T34 builds it against T33's state tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- SKELETON (T32): T34 reads the props.
export function PackOffer(_props: PackOfferProps): React.JSX.Element {
  return <div data-skeleton="PackOffer" />;
}
