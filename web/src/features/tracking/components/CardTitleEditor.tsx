import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useId, useRef, useState } from 'react';

import { useErrorHold } from '@/features/retry/useHold';

import {
  CANCEL_TITLE_LABEL,
  CLEAR_TITLE_LABEL,
  EDIT_TITLE_LABEL,
  RETITLE_PENDING_LABEL,
  SAVE_TITLE_LABEL,
  TITLE_INPUT_LABEL,
  TITLE_MAX_CHARACTERS,
  retitleFailureCopy,
  titleCounter,
} from '../trackingCopy';
import { retitleTrackedApplicationMutationKey } from '../hooks/trackingKeys';
import { useRetitleTrackedApplication } from '../hooks/useRetitleTrackedApplication';
import { variablesNameId } from '../hooks/boardCache';

export interface CardTitleEditorProps {
  /** The signed-in user — roots the retitle mutation's key. */
  readonly userId: string;
  /** The card being retitled. */
  readonly applicationId: string;
  /** The card's own title, `null` when it has none (the input then starts empty). */
  readonly title: string | null;
  /** The `version` the board shows — sent with the `PUT`. */
  readonly version: number;
  /**
   * The card's own move is on the wire. *Edit title*, **Save** and **Clear title** are disabled: a
   * retitle sent now would carry the pre-move `version` and be refused as a conflict.
   */
  readonly movePending?: boolean;
  /** The card's title element, so *Edit title* says which card it edits. */
  readonly describedBy?: string;
}

const BUTTON_CLASS =
  'rounded-md border border-slate-300 bg-white px-2 py-1 text-sm font-medium text-slate-800 disabled:opacity-60';

/**
 * **Edit title** → an inline, labelled text input (AC-36) — a **container** over
 * `useRetitleTrackedApplication`. The text being typed is local `useState` (form state), seeded from
 * the card's title when the editor opens; the counter is visible (*"n / 120"*); Enter or **Save**
 * sends the title as typed; **Clear title** sends `null`; Escape cancels and returns focus to
 * **Edit title**. A refusal keeps the editor open with what was typed: 422 shows the boundary's
 * message under the field, linked by `aria-describedby`; a 409 says the card changed (the board is
 * re-read, so the next Save sends the newer version). Not optimistic.
 *
 * `maxLength` and the counter are presentation for typing, not the rule: the server's
 * `ApplicationTitle` decides, and its 422 is what the user reads.
 *
 * Moving focus is the one effect here — the input does not exist until the editor opens, and the
 * *Edit title* button not while it is open, so focus can only be put on either after the commit
 * that renders it.
 */
export function CardTitleEditor({
  userId,
  applicationId,
  title,
  version,
  movePending = false,
  describedBy,
}: CardTitleEditorProps): React.JSX.Element {
  const retitle = useRetitleTrackedApplication(userId);
  // Copy only (AC-16): a 429's wait, worded once from when it arrived. Nothing is held.
  const retitleHold = useErrorHold(retitle.error);
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const returnFocus = useRef(false);
  const inputId = useId();
  const counterId = useId();
  const errorId = useId();

  const editing = draft !== null;

  // Focus follows the editor: into the field when it opens (the user asked for it), back to *Edit
  // title* when it closes. Neither element exists before the commit that renders it.
  useEffect(() => {
    if (editing) {
      input.current?.focus();
    } else if (returnFocus.current) {
      returnFocus.current = false;
      editButton.current?.focus();
    }
  }, [editing]);

  function close(): void {
    returnFocus.current = true;
    retitle.reset();
    setDraft(null);
  }

  function save(next: string | null): void {
    // `isPending` lags a same-tick double submit; the mutation cache does not (T-29).
    const mutationKey = retitleTrackedApplicationMutationKey(userId);
    const inFlight = queryClient.isMutating({
      mutationKey,
      predicate: (mutation) => variablesNameId(mutation.state.variables, applicationId),
    });
    if (inFlight > 0 || movePending) {
      return;
    }
    retitle.mutate({ id: applicationId, title: next, version }, { onSuccess: close });
  }

  if (!editing) {
    return (
      <button
        ref={editButton}
        type="button"
        aria-describedby={describedBy}
        disabled={movePending}
        onClick={() => {
          setDraft(title ?? '');
        }}
        className="text-sm text-slate-700 underline underline-offset-2 disabled:text-slate-400 disabled:no-underline"
      >
        {EDIT_TITLE_LABEL}
      </button>
    );
  }

  const failure = retitle.isError
    ? retitleFailureCopy(retitle.error, retitleHold.deadlineMs, retitleHold.receivedAtMs)
    : null;

  return (
    <form
      className="space-y-1"
      onSubmit={(event) => {
        event.preventDefault();
        save(draft);
      }}
    >
      <label htmlFor={inputId} className="block text-xs font-medium text-slate-600">
        {TITLE_INPUT_LABEL}
      </label>
      <input
        id={inputId}
        type="text"
        value={draft}
        ref={input}
        maxLength={TITLE_MAX_CHARACTERS}
        aria-invalid={failure !== null}
        aria-describedby={failure === null ? counterId : `${counterId} ${errorId}`}
        onChange={(event) => {
          setDraft(event.target.value);
        }}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.preventDefault();
            close();
          }
        }}
        className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
      />
      <p id={counterId} className="text-xs text-slate-500">
        {titleCounter(draft.length)}
      </p>
      {failure !== null && (
        <p id={errorId} role="alert" className="text-sm text-red-700">
          {failure}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={retitle.isPending || movePending} className={BUTTON_CLASS}>
          {retitle.isPending ? RETITLE_PENDING_LABEL : SAVE_TITLE_LABEL}
        </button>
        <button
          type="button"
          disabled={retitle.isPending || movePending}
          onClick={() => {
            save(null);
          }}
          className={BUTTON_CLASS}
        >
          {CLEAR_TITLE_LABEL}
        </button>
        <button type="button" onClick={close} className={BUTTON_CLASS}>
          {CANCEL_TITLE_LABEL}
        </button>
      </div>
    </form>
  );
}
