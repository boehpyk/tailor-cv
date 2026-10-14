export interface PurgePanelProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

/**
 * The purge (AC-37): facts from `useReadiness`, *Preview purge*, *Run purge* with its confirm, 409/503 copy.
 *
 * SKELETON (T20): an empty, data-attributed placeholder; T22 builds it.
 */
export function PurgePanel({ actorId }: PurgePanelProps): React.JSX.Element {
  return <div data-admin-stub="purge-panel" data-actor={actorId} />;
}
