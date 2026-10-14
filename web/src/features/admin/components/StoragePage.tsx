import { JobHealth } from './JobHealth';
import { PurgePanel } from './PurgePanel';
import { StorageTable } from './StorageTable';
import { SweepPanel } from './SweepPanel';

export interface StoragePageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

/**
 * `/admin/storage` (AC-36 … AC-39): the storage table, the purge and sweep panels and job health.
 *
 * SKELETON (T20): composes the stubs; T22 adds the heading and sections.
 */
export function StoragePage({ actorId }: StoragePageProps): React.JSX.Element {
  return (
    <div data-admin-stub="storage">
      <StorageTable actorId={actorId} />
      <PurgePanel actorId={actorId} />
      <SweepPanel actorId={actorId} />
      <JobHealth />
    </div>
  );
}
