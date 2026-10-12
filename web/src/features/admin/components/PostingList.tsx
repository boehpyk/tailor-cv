export interface PostingListProps {
  readonly actorId: string;
  /** The account whose content this lists. */
  readonly userId: string;
}

/**
 * The account's 20 most recent job postings (AC-26, OQ-6): loading / error + Retry / *No job
 * postings.* / rows linking to each posting's page; *Showing the 20 most recent.* when full.
 *
 * SKELETON (T11): an empty, distinguishable placeholder — no text and no role, so 4.2's
 * `UserPage` tests (which assert no "Loading…" once the overview shows) stay green; T13 builds the states.
 */
export function PostingList({ actorId, userId }: PostingListProps): React.JSX.Element {
  return <section data-content="postings" data-actor={actorId} data-user={userId} />;
}
