import { useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';

import { renameSavedBaseCvMutationKey } from '../hooks/savedCvsKeys';
import { useRenameSavedBaseCv } from '../hooks/useRenameSavedBaseCv';
import {
  CANCEL_LABEL,
  RENAME_INPUT_LABEL,
  RENAME_PENDING_LABEL,
  RENAME_SAVE_LABEL,
  renameSavedCvErrorCopy,
} from '../savedCvsCopy';

import type { SavedBaseCv } from '../types';

export interface RenameLabelFormProps {
  /** The CV being renamed; its current label (or none) seeds the input. */
  readonly cv: SavedBaseCv;
  /** The form is finished — saved, or cancelled. The section stops rendering it. */
  readonly onDone: () => void;
}

/**
 * The inline rename form (AC-37). The draft is **local form state** (`useState`), seeded from the
 * CV once and never synced back from the query. Owns its own `useRenameSavedBaseCv` so its pending
 * and error are its own: a labelled input, Save / Cancel, "Saving…" while pending, and a refusal
 * (`invalid_label` and the rest) as `role="alert"` linked to the input by `aria-describedby`.
 *
 * The label is sent as typed — no trimming, no length check, no `maxLength`: those are the server's
 * rules (`BaseCvLabel`) and its 422 is what the user sees. The one thing decided here is what an
 * **empty** input means, which is a question about the form, not the rule: "no name", sent as `null`.
 */
export function RenameLabelForm({ cv, onDone }: RenameLabelFormProps): React.JSX.Element {
  const rename = useRenameSavedBaseCv();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(cv.label ?? '');
  const inputId = useId();
  const errorId = useId();

  function handleSubmit(event: React.SyntheticEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (queryClient.isMutating({ mutationKey: renameSavedBaseCvMutationKey }) > 0) {
      return;
    }
    rename.mutate({ id: cv.id, label: draft === '' ? null : draft }, { onSuccess: onDone });
  }

  const error = rename.error;

  return (
    <form onSubmit={handleSubmit} className="space-y-2">
      <label htmlFor={inputId} className="block text-sm font-medium text-slate-700">
        {RENAME_INPUT_LABEL}
      </label>
      <input
        id={inputId}
        type="text"
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
        }}
        aria-describedby={error === null ? undefined : errorId}
        aria-invalid={error === null ? undefined : true}
        className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
      />
      {error !== null && (
        <p id={errorId} role="alert" className="text-sm text-red-700">
          {renameSavedCvErrorCopy(error)}
        </p>
      )}
      <div className="flex gap-3">
        <button
          type="submit"
          disabled={rename.isPending}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-60"
        >
          {rename.isPending ? RENAME_PENDING_LABEL : RENAME_SAVE_LABEL}
        </button>
        <button
          type="button"
          onClick={onDone}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          {CANCEL_LABEL}
        </button>
      </div>
    </form>
  );
}
