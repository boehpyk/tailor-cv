export interface HistoryListProps {
  readonly actorId: string;
  /** The account whose content this lists. */
  readonly userId: string;
}

/**
 * The account's history (AC-26): loading / error + Retry / *No tailoring history.* / rows linking
 * to each run's page, *Load more* while `next_cursor` is set.
 *
 * SKELETON (T11): an empty, distinguishable placeholder — no text and no role, so 4.2's
 * `UserPage` tests (which assert no "Loading…" once the overview shows) stay green; T13 builds the states.
 */
export function HistoryList({ actorId, userId }: HistoryListProps): React.JSX.Element {
  return <section data-content="history" data-actor={actorId} data-user={userId} />;
}
