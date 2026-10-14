import { useMutation } from '@tanstack/react-query';

import { downloadUserDocument, downloadUserExportFile } from '@/api/admin';
import { downloadFilenameFor } from '@/features/export/exportCopy';
import { saveBlob } from '@/features/export/saveBlob';

import type { AdminSubject } from '@/api/admin';
import type { ExportJob, InlineExportFormat } from '@/features/export/types';
import type { TailoredDocumentKind } from '@/features/tailoring/types';
import type { UseMutationResult } from '@tanstack/react-query';

/** What to download: a document rendered in the request, or an export job's stored file. */
export type AdminDownload =
  | {
      readonly source: 'document';
      readonly runId: string;
      readonly document: TailoredDocumentKind;
      readonly format: InlineExportFormat;
    }
  | { readonly source: 'export'; readonly job: ExportJob };

async function download(subject: AdminSubject, target: AdminDownload): Promise<void> {
  if (target.source === 'document') {
    const blob = await downloadUserDocument(subject, target.runId, target.document, target.format);
    saveBlob(blob, downloadFilenameFor(target.document, target.format));
    return;
  }
  const blob = await downloadUserExportFile(subject, target.job.id);
  saveBlob(blob, downloadFilenameFor(target.job.document, target.job.format));
}

/**
 * One download of the subject's content, through the typed client and never an `<a href>` (AC-28):
 * a refusal (410 `export_file_gone`, 409, 503) is the mutation's `error`, shown in place, and the
 * button is usable again. Nothing retries by itself (ADR-0031). The filename is the existing
 * constant per (document, format), never the user's text.
 */
export function useAdminDownload(
  subject: AdminSubject,
): UseMutationResult<void, Error, AdminDownload> {
  return useMutation({ mutationFn: (target: AdminDownload) => download(subject, target) });
}
