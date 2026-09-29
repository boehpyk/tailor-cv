import { useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { Link } from 'react-router';

import { useAuth } from '@/features/auth/hooks/useAuth';

import { copySavedBaseCvMutationKey } from '../hooks/savedCvsKeys';
import { useCopySavedBaseCv } from '../hooks/useCopySavedBaseCv';
import { useSavedBaseCvs } from '../hooks/useSavedBaseCvs';
import {
  PICKER_EMPTY_ACTION,
  PICKER_EMPTY_NOTE,
  PICKER_LEGEND,
  PICKER_RETENTION_NOTICE,
  PICKER_UNAVAILABLE_NOTE,
  RETRY_LABEL,
  SAVED_CVS_LOADING_NOTE,
  SAVED_CVS_LOAD_ERROR_NOTE,
  SAVED_CV_STATUS_LABELS,
  USE_THIS_CV_LABEL,
  USE_THIS_CV_PENDING_LABEL,
  copySavedCvErrorCopy,
} from '../savedCvsCopy';

import type { SavedBaseCv } from '../types';

const buttonClass =
  'rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-60';

/**
 * How the picker is used (slice 2.3, plan §7):
 *
 * - **`'copy'`** — 2.2's picker in the guest workspace's base-CV tab: *Use this CV* copies the chosen
 *   saved CV into the workspace (`POST /api/base-cvs/copies`). The default, so every existing caller
 *   is unchanged.
 * - **`'select'`** — the account workspace's (AC-39): the selection **is** the run's base CV, a saved
 *   CV referenced directly, so nothing is copied and no copy request is ever made. Controlled: the
 *   workspace owns `chosenId` (form state, `useState`) and validates it against the list on render.
 */
export type SavedBaseCvPickerProps =
  | { readonly mode?: 'copy' }
  | {
      readonly mode: 'select';
      /** The user's choice, as the workspace holds it — possibly stale; the list decides. */
      readonly chosenId: string | null;
      readonly onChoose: (id: string) => void;
    };

/**
 * The saved-CV picker (2.2's AC-38, AC-39, AC-45; 2.3's AC-39) — a container.
 *
 * Gated on the auth state first: **nothing** while `anonymous` (a guest never sees it) and nothing
 * while `booting` (no flicker); `unavailable` → "Couldn't check your account…" + Retry. Only when
 * `authenticated` does it read the list — the gate is a component boundary, so the list hook is not
 * even called for a guest.
 */
export function SavedBaseCvPicker(props: SavedBaseCvPickerProps = {}): React.JSX.Element | null {
  const auth = useAuth();

  switch (auth.status) {
    case 'anonymous':
    case 'booting':
      return null;
    case 'unavailable':
      return (
        <div className="mb-6 space-y-2 rounded-lg border border-slate-200 p-4">
          <p className="text-sm text-slate-700">{PICKER_UNAVAILABLE_NOTE}</p>
          <button type="button" onClick={auth.retry} className={buttonClass}>
            {RETRY_LABEL}
          </button>
        </div>
      );
    case 'authenticated':
      return props.mode === 'select' ? <SelectPicker /> : <AuthenticatedPicker />;
  }
}

/**
 * `mode: 'select'` (AC-39): 2.2's list states and radiogroup, preselected only when exactly one CV
 * is `extracted`, `extraction_failed` CVs listed and disabled with their reason, an inline *Upload a
 * CV to your account*, and an empty state that offers the upload — with no *Use this CV* button and
 * no copy.
 *
 * SKELETON (T29): a distinguishable stub; T31 builds it.
 */
function SelectPicker(): React.JSX.Element {
  return <p>SavedBaseCvPicker select mode (skeleton)</p>;
}

/**
 * Which CV the button would copy: the user's choice if it is still in the list and copyable, else
 * the only CV when there is exactly one (AC-38), else none. Derived on every render from the id in
 * `useState` and the query's `items`, so a CV deleted in another tab can never stay selected.
 */
function effectiveSelection(items: readonly SavedBaseCv[], chosenId: string | null): string | null {
  const copyable = items.filter((cv) => cv.status === 'extracted');
  if (chosenId !== null && copyable.some((cv) => cv.id === chosenId)) {
    return chosenId;
  }
  const [only] = items;
  return items.length === 1 && only?.status === 'extracted' ? only.id : null;
}

/**
 * The authenticated picker: the query's own states — loading, error + Retry, empty (a link to
 * `/account`), or a `radiogroup` with a `legend`, in the server's order (newest first),
 * `extraction_failed` CVs listed but disabled with their reason — plus **Use this CV** and AC-45's
 * retention statement.
 *
 * The copy's refusal is rendered **outside** the list's states on purpose: a 404 refetches the list,
 * which may then be empty, and "that saved CV was deleted" has to survive that.
 */
function AuthenticatedPicker(): React.JSX.Element {
  const list = useSavedBaseCvs();
  const copy = useCopySavedBaseCv();
  const queryClient = useQueryClient();
  const [chosenId, setChosenId] = useState<string | null>(null);
  const legendId = useId();
  const errorId = useId();
  const radioName = useId();

  function body(): React.JSX.Element {
    if (list.isPending) {
      return (
        <p role="status" className="text-sm text-slate-500">
          {SAVED_CVS_LOADING_NOTE}
        </p>
      );
    }
    if (list.isError) {
      return (
        <div role="alert" className="space-y-2">
          <p className="text-sm text-slate-700">{SAVED_CVS_LOAD_ERROR_NOTE}</p>
          <button
            type="button"
            onClick={() => {
              void list.refetch();
            }}
            className={buttonClass}
          >
            {RETRY_LABEL}
          </button>
        </div>
      );
    }
    const items = list.data.items;
    if (items.length === 0) {
      return (
        <p className="text-sm text-slate-600">
          <span>{PICKER_EMPTY_NOTE}</span>{' '}
          <Link to="/account" className="font-medium text-slate-900 underline underline-offset-2">
            {PICKER_EMPTY_ACTION}
          </Link>
        </p>
      );
    }

    const selectedId = effectiveSelection(items, chosenId);

    function copySelected(): void {
      // `isPending` lags a same-tick double click; the mutation cache does not (AC-39, CLAUDE.md).
      if (
        selectedId === null ||
        queryClient.isMutating({ mutationKey: copySavedBaseCvMutationKey }) > 0
      ) {
        return;
      }
      copy.mutate(selectedId);
    }

    return (
      <div className="space-y-3">
        <fieldset role="radiogroup" aria-labelledby={legendId} className="space-y-2">
          <legend id={legendId} className="mb-2 text-sm font-medium text-slate-900">
            {PICKER_LEGEND}
          </legend>
          {items.map((cv) => {
            const copyable = cv.status === 'extracted';
            const reasonId = `${radioName}-${cv.id}-reason`;
            return (
              <div key={cv.id} className="flex items-start gap-2">
                <input
                  id={`${radioName}-${cv.id}`}
                  type="radio"
                  name={radioName}
                  value={cv.id}
                  checked={selectedId === cv.id}
                  disabled={!copyable}
                  aria-describedby={copyable ? undefined : reasonId}
                  onChange={() => {
                    setChosenId(cv.id);
                  }}
                  className="mt-1"
                />
                <label htmlFor={`${radioName}-${cv.id}`} className="text-sm">
                  <span className={copyable ? 'text-slate-900' : 'text-slate-400'}>
                    {cv.label ?? cv.original_filename}
                  </span>
                  {!copyable && (
                    <span id={reasonId} className="block text-slate-500">
                      {cv.failure_message ?? SAVED_CV_STATUS_LABELS[cv.status]}
                    </span>
                  )}
                </label>
              </div>
            );
          })}
        </fieldset>
        <p className="text-sm text-slate-500">{PICKER_RETENTION_NOTICE}</p>
        <button
          type="button"
          onClick={copySelected}
          disabled={selectedId === null || copy.isPending}
          aria-describedby={copy.error === null ? undefined : errorId}
          className={buttonClass}
        >
          {copy.isPending ? USE_THIS_CV_PENDING_LABEL : USE_THIS_CV_LABEL}
        </button>
      </div>
    );
  }

  return (
    <div className="mb-6 space-y-3 rounded-lg border border-slate-200 p-4">
      {body()}
      {copy.error !== null && (
        <p id={errorId} role="alert" className="text-sm text-red-700">
          {copySavedCvErrorCopy(copy.error)}
        </p>
      )}
    </div>
  );
}
