export interface StorageTableProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

/**
 * The five buckets from `useStorage` (AC-36): loading / error + Retry / table, *Measured at*, *Measure again*.
 *
 * SKELETON (T20): an empty, data-attributed placeholder; T22 builds it.
 */
export function StorageTable({ actorId }: StorageTableProps): React.JSX.Element {
  return <div data-admin-stub="storage-table" data-actor={actorId} />;
}
