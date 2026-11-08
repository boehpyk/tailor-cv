export interface GuestTailorOfferProps {
  /**
   * `'workspace'`: below the guest's fit score. `'run'`: on a guest run page, where the export bar
   * was (AC-36). Copy only; both link to `/register`.
   */
  readonly placement: 'workspace' | 'run';
}

/**
 * The guest's offer (AC-36): create a free account, and the first tailoring is free. Static.
 *
 * SKELETON (T32): renders a placeholder. T34 builds it against T33's state tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- SKELETON (T32): T34 reads the props.
export function GuestTailorOffer(_props: GuestTailorOfferProps): React.JSX.Element {
  return <div data-skeleton="GuestTailorOffer" />;
}
