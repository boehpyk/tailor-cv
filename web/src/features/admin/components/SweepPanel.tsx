export interface SweepPanelProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

/**
 * The orphan sweep (AC-38): *Preview sweep*, *Run sweep* with its confirm, 503 copy.
 *
 * SKELETON (T20): an empty, data-attributed placeholder; T22 builds it.
 */
export function SweepPanel({ actorId }: SweepPanelProps): React.JSX.Element {
  return <div data-admin-stub="sweep-panel" data-actor={actorId} />;
}
