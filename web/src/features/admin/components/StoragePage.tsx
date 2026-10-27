import { STORAGE_HEADING } from '../adminCopy';

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
 * Each part has its own query and its own states, so one failing leaves the others usable.
 */
export function StoragePage({ actorId }: StoragePageProps): React.JSX.Element {
  return (
    <section aria-labelledby="admin-storage-heading" className="mb-10 min-w-0 space-y-8">
      <h1 id="admin-storage-heading" className="font-display text-xl font-bold text-slate-900">
        {STORAGE_HEADING}
      </h1>
      <StorageTable actorId={actorId} />
      <PurgePanel actorId={actorId} />
      <SweepPanel actorId={actorId} />
      <JobHealth />
    </section>
  );
}
