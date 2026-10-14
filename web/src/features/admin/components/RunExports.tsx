import { EXPORT_FORMAT_LABELS } from '@/features/export/exportCopy';
import { layoutNameOf } from '@/features/export/layouts';

import {
  DOCUMENT_HEADINGS,
  EXPORTS_EMPTY,
  EXPORTS_ERROR,
  EXPORTS_HEADING,
  EXPORT_DOWNLOAD_LABEL,
  EXPORT_OLDER_VERSION,
  EXPORT_STATUS_LABELS,
  USER_LOADING,
} from '../adminCopy';
import { useAdminRunExports } from '../hooks/useAccountContent';
import { downloadFailureCopy } from '../contentView';
import { useAdminDownload } from '../hooks/useAdminDownload';

import { ContentSection, EMPTY } from './ContentSection';

import type { AdminSubject } from '@/api/admin';
import type { ExportJob } from '@/features/export/types';

export interface RunExportsProps {
  readonly actorId: string;
  readonly subject: AdminSubject;
  readonly runId: string;
}

function describe(job: ExportJob): string {
  const parts = [DOCUMENT_HEADINGS[job.document], EXPORT_FORMAT_LABELS[job.format]];
  if (job.layout_template !== null) parts.push(layoutNameOf(job.layout_template));
  parts.push(EXPORT_STATUS_LABELS[job.status]);
  return parts.join(' · ');
}

/**
 * A run's export jobs (AC-28): one row per job — document, format, layout, status — and
 * *Download* only for a ready one, labelled *older version* when the run was edited since
 * (`current` is the server's answer, never re-derived). A refused download is shown on its row;
 * the button is usable again.
 */
export function RunExports({ actorId, subject, runId }: RunExportsProps): React.JSX.Element {
  const exports = useAdminRunExports(actorId, subject, runId);
  const download = useAdminDownload(subject);
  const items = exports.data?.items ?? [];
  const failedJobId =
    download.isError && download.variables.source === 'export' ? download.variables.job.id : null;

  return (
    <ContentSection
      id="admin-run-exports"
      heading={EXPORTS_HEADING}
      query={exports}
      loadingText={USER_LOADING}
      errorText={EXPORTS_ERROR}
    >
      {items.length === 0 ? (
        <p className={EMPTY}>{EXPORTS_EMPTY}</p>
      ) : (
        <ul className="space-y-2 text-sm">
          {items.map((job) => (
            <li key={job.id} className="space-y-1">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="text-slate-800">{describe(job)}</span>
                {job.status === 'ready' && !job.current && (
                  <span className="text-slate-600">{EXPORT_OLDER_VERSION}</span>
                )}
                {job.status === 'ready' && (
                  <button
                    type="button"
                    disabled={download.isPending}
                    onClick={() => {
                      download.mutate({ source: 'export', job });
                    }}
                    className="rounded-md border border-slate-300 px-3 py-1.5 font-medium text-slate-900 disabled:opacity-60"
                  >
                    {EXPORT_DOWNLOAD_LABEL}
                  </button>
                )}
              </div>
              {failedJobId === job.id && download.isError && (
                <p role="alert" className="text-red-700">
                  {downloadFailureCopy(download.error)}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </ContentSection>
  );
}
