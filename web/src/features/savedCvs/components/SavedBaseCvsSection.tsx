import { useId, useState } from 'react';

import { useDeleteSavedBaseCv } from '../hooks/useDeleteSavedBaseCv';
import { useSavedBaseCvs } from '../hooks/useSavedBaseCvs';
import {
  ALREADY_DELETED_NOTE,
  RETRY_LABEL,
  SAVED_CVS_EMPTY_NOTE,
  SAVED_CVS_HEADING,
  SAVED_CVS_LOADING_NOTE,
  SAVED_CVS_LOAD_ERROR_NOTE,
  SAVED_CV_RETENTION_NOTICE,
  deleteSavedCvErrorCopy,
} from '../savedCvsCopy';

import { ConfirmDeleteDialog } from './ConfirmDeleteDialog';
import { RenameLabelForm } from './RenameLabelForm';
import { SavedBaseCvRow } from './SavedBaseCvRow';
import { SavedCvUploadControl } from './SavedCvUploadControl';

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

      <SavedCvUploadControl notice={SAVED_CV_RETENTION_NOTICE} />
    </div>
  );
}
