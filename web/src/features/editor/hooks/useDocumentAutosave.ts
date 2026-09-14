import type { DocumentEditorHandle } from './useDocumentEditor';
import type { SaveState } from '../saveState';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

/**
 * Keep one document saved — the slice's teaching hook (technical plan, "The editor").
 *
 * When built (F10c) it owns: a `useMutation` over `reviseTailoredDocument` scoped per run
 * (`scope: { id: \`revise:${runId}\` }`, AC-32), a debounce timer in a ref armed by the editor's
 * `update` event and flushed by a tab switch and `visibilitychange`, the `expected_version` read
 * from the query cache **at send time**, `setQueryData` on 200, and the 409 / 401 / 422 / 429
 * resolutions. It returns the save state and nothing else; what the indicator says about it is
 * `saveStateCopy`'s business.
 *
 * Skeleton (F8): always `saved`. No timer, no mutation, no request — a document opened against
 * this hook issues nothing, which is also what AC-29 requires of the finished one.
 */
/* eslint-disable @typescript-eslint/no-unused-vars -- skeleton: every parameter is read in F10c */
export function useDocumentAutosave(
  _runId: string,
  _kind: TailoredDocumentKind,
  _handle: DocumentEditorHandle,
): SaveState {
  return { kind: 'saved' };
}
/* eslint-enable @typescript-eslint/no-unused-vars */
