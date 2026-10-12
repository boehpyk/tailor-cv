export interface SavedCvListProps {
  readonly actorId: string;
  /** The account whose content this lists. */
  readonly userId: string;
}

/**
 * The account's saved CVs (AC-26): loading / error + Retry / *No saved CVs.* / rows linking to
 * each CV's page.
 *
 * SKELETON (T11): an empty, distinguishable placeholder — no text and no role, so 4.2's
 * `UserPage` tests (which assert no "Loading…" once the overview shows) stay green; T13 builds the states.
 */
export function SavedCvList({ actorId, userId }: SavedCvListProps): React.JSX.Element {
  return <section data-content="cvs" data-actor={actorId} data-user={userId} />;
}
