import { useId } from 'react';

import { useUploadSavedBaseCv } from '../hooks/useUploadSavedBaseCv';
import {
  UPLOAD_SAVED_CV_LABEL,
  UPLOAD_SAVED_CV_PENDING_LABEL,
  uploadSavedCvErrorCopy,
} from '../savedCvsCopy';

export interface SavedCvUploadControlProps {
  /**
   * The line shown before a file is chosen. `/account` states AC-44's retention notice; the account
   * workspace states its own (slice 2.3, AC-49 — no "24 hours" in account scope).
   */
  readonly notice: string;
}

/**
 * The account upload (`POST /api/me/base-cvs`): a labelled file input, a notice before anything is chosen,
 * the pending line while the request (which includes the extraction) is out, and the refusal in its
 * own words. No client-side size or format check: every refusal has copy, and the only rule that
 * could be mirrored here without a round trip — the cap — must not be (AC-35).
 */
export function SavedCvUploadControl({ notice }: SavedCvUploadControlProps): React.JSX.Element {
  const upload = useUploadSavedBaseCv();
  const inputId = useId();
  const errorId = useId();
  const error = upload.error;

  function handleChange(event: React.ChangeEvent<HTMLInputElement>): void {
    const file = event.target.files?.[0];
    // Reset, so choosing the same file again after a refusal fires another change.
    event.target.value = '';
    if (file !== undefined) {
      upload.mutate(file);
    }
  }

  return (
    <div className="space-y-2 rounded-lg border border-dashed border-slate-300 p-4">
      <label htmlFor={inputId} className="block text-sm font-medium text-slate-700">
        {UPLOAD_SAVED_CV_LABEL}
      </label>
      <p className="text-sm text-slate-500">{notice}</p>
      <input
        id={inputId}
        type="file"
        accept=".pdf,.docx,.txt"
        disabled={upload.isPending}
        onChange={handleChange}
        aria-describedby={error === null ? undefined : errorId}
        className="block text-sm"
      />
      {upload.isPending && (
        <p role="status" className="text-sm font-medium text-slate-500">
          {UPLOAD_SAVED_CV_PENDING_LABEL}
        </p>
      )}
      {error !== null && (
        <p id={errorId} role="alert" className="text-sm text-red-700">
          {uploadSavedCvErrorCopy(error, null)}
        </p>
      )}
    </div>
  );
}
