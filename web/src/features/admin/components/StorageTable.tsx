import {
  MEASURE_AGAIN_LABEL,
  STORAGE_BUCKET_LABELS,
  STORAGE_CAPTION,
  STORAGE_COLUMN_BUCKET,
  STORAGE_COLUMN_FILES,
  STORAGE_COLUMN_SIZE,
  STORAGE_ERROR,
  STORAGE_LOADING,
  STORAGE_UNREFERENCED_NOTE,
  storageMeasuredAt,
} from '../adminCopy';
import { useStorage } from '../hooks/useStorage';

import { ErrorWithRetry } from './ContentPage';

export interface StorageTableProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

const BUCKETS = Object.keys(STORAGE_BUCKET_LABELS) as (keyof typeof STORAGE_BUCKET_LABELS)[];

/** Decimal kB / MB / GB in the browser's locale (AC-36). */
function formatBytes(bytes: number): string {
  const [unit, size] =
    bytes >= 1e9
      ? (['gigabyte', 1e9] as const)
      : bytes >= 1e6
        ? (['megabyte', 1e6] as const)
        : (['kilobyte', 1e3] as const);
  return new Intl.NumberFormat(undefined, {
    style: 'unit',
    unit,
    maximumFractionDigits: 1,
  }).format(bytes / size);
}

/**
 * The five buckets from `useStorage` (AC-36): loading / error + Retry / the table, *Measured at*
 * and *Measure again* (a refetch; disabled while one is in flight, the last answer stays shown).
 */
export function StorageTable({ actorId }: StorageTableProps): React.JSX.Element {
  const storage = useStorage(actorId);
  const { data } = storage;
  if (data === undefined) {
    return storage.error !== null ? (
      <ErrorWithRetry text={STORAGE_ERROR} query={storage} />
    ) : (
      <p role="status" className="text-slate-600">
        {STORAGE_LOADING}
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{STORAGE_CAPTION}</caption>
        <thead className="border-b border-slate-200 text-slate-600">
          <tr>
            <th scope="col" className="py-2 pr-4 font-medium">
              {STORAGE_COLUMN_BUCKET}
            </th>
            <th scope="col" className="py-2 pr-4 font-medium">
              {STORAGE_COLUMN_FILES}
            </th>
            <th scope="col" className="py-2 font-medium">
              {STORAGE_COLUMN_SIZE}
            </th>
          </tr>
        </thead>
        <tbody>
          {BUCKETS.map((bucket) => (
            <tr key={bucket} className="border-b border-slate-100">
              <th scope="row" className="py-2 pr-4 font-normal">
                {STORAGE_BUCKET_LABELS[bucket]}
              </th>
              <td className="py-2 pr-4">{data[bucket].files.toLocaleString()}</td>
              <td className="py-2">{formatBytes(data[bucket].bytes)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-sm text-slate-600">{STORAGE_UNREFERENCED_NOTE}</p>
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm text-slate-600">
          {storageMeasuredAt(
            new Date(data.scanned_at).toLocaleTimeString(undefined, {
              hour: '2-digit',
              minute: '2-digit',
            }),
          )}
        </p>
        <button
          type="button"
          onClick={() => {
            void storage.refetch();
          }}
          disabled={storage.isFetching}
          className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-900 disabled:opacity-60"
        >
          {MEASURE_AGAIN_LABEL}
        </button>
      </div>
    </div>
  );
}
