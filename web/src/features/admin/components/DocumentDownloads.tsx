import type { TailoredDocumentKind } from '@/features/tailoring/types';

export interface DocumentDownloadsProps {
  readonly userId: string;
  readonly runId: string;
  readonly document: TailoredDocumentKind;
}

/**
 * *Download .md* / *Download .txt* for one document of a **succeeded** run (AC-28), through
 * `useAdminDownload`; a refusal is an alert in place and the button is usable again.
 *
 * SKELETON (T11): a distinguishable placeholder.
 */
export function DocumentDownloads({
  userId,
  runId,
  document,
}: DocumentDownloadsProps): React.JSX.Element {
  return <div data-user={userId} data-run={runId} data-document={document} />;
}
