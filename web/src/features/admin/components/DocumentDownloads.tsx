import { DOWNLOAD_INLINE_LABELS } from '../adminCopy';
import { downloadFailureCopy } from '../contentView';
import { useAdminDownload } from '../hooks/useAdminDownload';

import type { AdminSubject } from '@/api/admin';
import type { InlineExportFormat } from '@/features/export/types';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

export interface DocumentDownloadsProps {
  readonly subject: AdminSubject;
  readonly runId: string;
  readonly document: TailoredDocumentKind;
}

const FORMATS: readonly InlineExportFormat[] = ['md', 'txt'];

/**
 * *Download .md* / *Download .txt* for one document of a **succeeded** run (AC-28), through
 * `useAdminDownload` — bytes via the typed client, never an `<a href>`. A refusal is an alert in
 * place; the buttons are usable again and nothing re-sends by itself (ADR-0031).
 */
export function DocumentDownloads({
  subject,
  runId,
  document,
}: DocumentDownloadsProps): React.JSX.Element {
  const download = useAdminDownload(subject);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {FORMATS.map((format) => (
          <button
            key={format}
            type="button"
            disabled={download.isPending}
            onClick={() => {
              download.mutate({ source: 'document', runId, document, format });
            }}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-900 disabled:opacity-60"
          >
            {DOWNLOAD_INLINE_LABELS[format]}
          </button>
        ))}
      </div>
      {download.isError && (
        <p role="alert" className="text-sm text-red-700">
          {downloadFailureCopy(download.error)}
        </p>
      )}
    </div>
  );
}
