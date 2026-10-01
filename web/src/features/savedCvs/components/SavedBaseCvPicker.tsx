import { useId } from 'react';

import { useAuth } from '@/features/auth/hooks/useAuth';

import { useSavedBaseCvs } from '../hooks/useSavedBaseCvs';
import {
  PICKER_UNAVAILABLE_NOTE,
  SELECT_PICKER_EMPTY_NOTE,
  SELECT_PICKER_LEGEND,
  WORKSPACE_UPLOAD_NOTICE,
  RETRY_LABEL,
  SAVED_CVS_LOADING_NOTE,
  SAVED_CVS_LOAD_ERROR_NOTE,
  SAVED_CV_STATUS_LABELS,
} from '../savedCvsCopy';
import { effectiveSelection } from '../selection';

import { SavedCvUploadControl } from './SavedCvUploadControl';

import type { SavedBaseCv } from '../types';

const buttonClass =
  'rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-60';

/**
 * The picker's one mode (slice 2.3's AC-39; 2.4 removed 2.2's `'copy'` mode, AC-44): the selection
 * **is** the run's base CV, a saved CV referenced directly, so nothing is copied. Controlled: the
 * account workspace owns `chosenId` (form state, `useState`) and validates it against the list on
 * render. `mode` stays as a literal so a call site says what it gets.
 */
export interface SavedBaseCvPickerProps {
  readonly mode: 'select';
  /** The user's choice, as the workspace holds it — possibly stale; the list decides. */
  readonly chosenId: string | null;
  readonly onChoose: (id: string) => void;
}

/**
 * The saved-CV picker (2.2's AC-38; 2.3's AC-39) — a container.
 *
 * Gated on the auth state first: **nothing** while `anonymous` (a guest never sees it) and nothing
 * while `booting` (no flicker); `unavailable` → "Couldn't check your account…" + Retry. Only when
 * `authenticated` does it read the list — the gate is a component boundary, so the list hook is not
 * even called for a guest.
 */
export function SavedBaseCvPicker({
  chosenId,
  onChoose,
}: SavedBaseCvPickerProps): React.JSX.Element | null {
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
      return <SelectPicker chosenId={chosenId} onChoose={onChoose} />;
  }
}

/**
 * `mode: 'select'` (AC-39): 2.2's list states and radiogroup — preselected only when exactly one CV
 * is `extracted`, `extraction_failed` CVs listed and disabled with their reason — an inline *Upload a
 * CV to your account* (2.2's hook and copy), and an empty state that offers the upload. No *Use this
 * CV* and no copy: the selection **is** the run's base CV, referenced directly (plan §0.1).
 *
 * Controlled: the workspace owns `chosenId` and launches with the same `effectiveSelection`, so the
 * checked radio and the CV a run uses cannot differ.
 */
function SelectPicker({
  chosenId,
  onChoose,
}: {
  readonly chosenId: string | null;
  readonly onChoose: (id: string) => void;
}): React.JSX.Element {
  const list = useSavedBaseCvs();

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
      return <p className="text-sm text-slate-600">{SELECT_PICKER_EMPTY_NOTE}</p>;
    }
    return (
      <SavedCvRadioGroup
        legend={SELECT_PICKER_LEGEND}
        items={items}
        selectedId={effectiveSelection(items, chosenId)}
        onChoose={onChoose}
      />
    );
  }

  return (
    <div className="mb-6 space-y-3">
      {body()}
      {/* The upload stays available in every settled state — beside the list, or as the empty
          state's one action. Hidden while the list loads or failed: nothing to add it beside. */}
      {list.isSuccess && <SavedCvUploadControl notice={WORKSPACE_UPLOAD_NOTICE} />}
    </div>
  );
}

/**
 * The saved CVs as a `radiogroup` with a `legend`, in the server's order (newest first). A CV that
 * is not `extracted` is listed but disabled, with its reason as the radio's description. Each name is user text, rendered as text.
 */
function SavedCvRadioGroup({
  legend,
  items,
  selectedId,
  onChoose,
}: {
  readonly legend: string;
  readonly items: readonly SavedBaseCv[];
  readonly selectedId: string | null;
  readonly onChoose: (id: string) => void;
}): React.JSX.Element {
  const legendId = useId();
  const radioName = useId();
  return (
    <fieldset role="radiogroup" aria-labelledby={legendId} className="space-y-2">
      <legend id={legendId} className="mb-2 text-sm font-medium text-slate-900">
        {legend}
      </legend>
      {items.map((cv) => {
        const usable = cv.status === 'extracted';
        const reasonId = `${radioName}-${cv.id}-reason`;
        return (
          <div key={cv.id} className="flex items-start gap-2">
            <input
              id={`${radioName}-${cv.id}`}
              type="radio"
              name={radioName}
              value={cv.id}
              checked={selectedId === cv.id}
              disabled={!usable}
              aria-describedby={usable ? undefined : reasonId}
              onChange={() => {
                onChoose(cv.id);
              }}
              className="mt-1"
            />
            <label htmlFor={`${radioName}-${cv.id}`} className="text-sm">
              <span className={usable ? 'text-slate-900' : 'text-slate-400'}>
                {cv.label ?? cv.original_filename}
              </span>
              {!usable && (
                <span id={reasonId} className="block text-slate-500">
                  {cv.failure_message ?? SAVED_CV_STATUS_LABELS[cv.status]}
                </span>
              )}
            </label>
          </div>
        );
      })}
    </fieldset>
  );
}
