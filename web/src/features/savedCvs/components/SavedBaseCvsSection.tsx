import { useId, useState } from 'react';

import { useDeleteSavedBaseCv } from '../hooks/useDeleteSavedBaseCv';
import { useSavedBaseCvs } from '../hooks/useSavedBaseCvs';
import { useUploadSavedBaseCv } from '../hooks/useUploadSavedBaseCv';
import {
  ALREADY_DELETED_NOTE,
  RETRY_LABEL,
  SAVED_CVS_EMPTY_NOTE,
  SAVED_CVS_HEADING,
  SAVED_CVS_LOADING_NOTE,
  SAVED_CVS_LOAD_ERROR_NOTE,
  SAVED_CV_RETENTION_NOTICE,
  UPLOAD_SAVED_CV_LABEL,
  UPLOAD_SAVED_CV_PENDING_LABEL,
  deleteSavedCvErrorCopy,
  uploadSavedCvErrorCopy,
} from '../savedCvsCopy';

import { ConfirmDeleteDialog } from './ConfirmDeleteDialog';
import { RenameLabelForm } from './RenameLabelForm';
import { SavedBaseCvRow } from './SavedBaseCvRow';

import type { SavedBaseCv } from '../types';

/**
 * The Saved CVs section on `/account` (AC-34, AC-35, AC-36, AC-44) — a container.
 *
 * Owns the list query (`useSavedBaseCvs`), the account upload (`useUploadSavedBaseCv`) and the
 * delete (`useDeleteSavedBaseCv`), and renders four deliberately distinct states: loading
 * (`role="status"`), error (`role="alert"` + Retry — never the empty state), empty ("No saved CVs
 * yet" + the upload control) and success (one `SavedBaseCvRow` per CV + the upload control). The
 * upload control states AC-44's retention notice before a file is chosen, and stays enabled at the
 * cap — the server's 409 is rendered (AC-35).
 *
 * **Local state is only which row is being renamed and which is being confirmed** — ids, never
 * copies of a row. Everything about a delete's outcome is read from the mutation itself: its
 * `variables` say which row, `isPending` says "Deleting…", `error` says "Not deleted", and a
 * resolved `'already_gone'` says the polite note.
 *
 * Rendered inside `RequireAuth`, so it does not gate on auth itself (S-57 is 2.1's Retry).
 */
export function SavedBaseCvsSection(): React.JSX.Element {
  const headingId = useId();

  return (
    <section aria-labelledby={headingId} className="mt-10 border-t border-slate-200 pt-6">
      <h2 id={headingId} className="mb-4 text-lg font-semibold text-slate-900">
        {SAVED_CVS_HEADING}
      </h2>
      <SavedBaseCvsBody />
    </section>
  );
}

function SavedBaseCvsBody(): React.JSX.Element {
  const list = useSavedBaseCvs();
  const remove = useDeleteSavedBaseCv();
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  if (list.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        {SAVED_CVS_LOADING_NOTE}
      </p>
    );
  }

  if (list.isError) {
    return (
      <div role="alert" className="space-y-3">
        <p className="text-sm text-slate-700">{SAVED_CVS_LOAD_ERROR_NOTE}</p>
        <button
          type="button"
          onClick={() => {
            void list.refetch();
          }}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white"
        >
          {RETRY_LABEL}
        </button>
      </div>
    );
  }

  const items = list.data.items;
  // Validated against the list on every render: a row that has gone has no dialog.
  const confirming: SavedBaseCv | undefined = items.find((cv) => cv.id === confirmingId);

  function noticeFor(cv: SavedBaseCv): string | null {
    return remove.isError && remove.variables === cv.id
      ? deleteSavedCvErrorCopy(remove.error)
      : null;
  }

  return (
    <div className="space-y-4">
      {items.length === 0 ? (
        <p className="text-sm text-slate-600">{SAVED_CVS_EMPTY_NOTE}</p>
      ) : (
        <ul className="space-y-3">
          {items.map((cv) => (
            <SavedBaseCvRow
              key={cv.id}
              cv={cv}
              isDeleting={remove.isPending && remove.variables === cv.id}
              deleteDisabled={remove.isPending}
              notice={noticeFor(cv)}
              renameForm={
                renamingId === cv.id ? (
                  <RenameLabelForm
                    cv={cv}
                    onDone={() => {
                      setRenamingId(null);
                    }}
                  />
                ) : null
              }
              onRename={() => {
                setRenamingId(cv.id);
              }}
              onDelete={() => {
                setConfirmingId(cv.id);
              }}
            />
          ))}
        </ul>
      )}

      {remove.isSuccess && remove.data === 'already_gone' && (
        <p role="status" className="text-sm text-slate-600">
          {ALREADY_DELETED_NOTE}
        </p>
      )}

      {confirming !== undefined && (
        <ConfirmDeleteDialog
          name={confirming.label ?? confirming.original_filename}
          onCancel={() => {
            setConfirmingId(null);
          }}
          onConfirm={() => {
            setConfirmingId(null);
            remove.mutate(confirming.id);
          }}
        />
      )}

      <SavedCvUploadControl />
    </div>
  );
}

/**
 * The account upload: a labelled file input, AC-44's retention notice before anything is chosen,
 * the pending line while the request (which includes the extraction) is out, and the refusal in its
 * own words. No client-side size or format check: every refusal has copy, and the only rule that
 * could be mirrored here without a round trip — the cap — must not be (AC-35).
 */
function SavedCvUploadControl(): React.JSX.Element {
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
      <p className="text-sm text-slate-500">{SAVED_CV_RETENTION_NOTICE}</p>
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
          {uploadSavedCvErrorCopy(error)}
        </p>
      )}
    </div>
  );
}
