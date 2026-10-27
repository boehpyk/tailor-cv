import { useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { Link } from 'react-router';

import { ApiError } from '@/api/client';

import { DeleteHistoryEntryDialog } from './DeleteHistoryEntryDialog';
import { HISTORY_GRID_CLASS, HistoryEntryRow } from './HistoryEntryRow';
import {
  DELETE_ENTRY_ALREADY_GONE_NOTE,
  HISTORY_COLUMN_LABELS,
  HISTORY_DELETION_NOTE,
  HISTORY_EMPTY_ACTION,
  HISTORY_EMPTY_NOTE,
  HISTORY_HEADING,
  HISTORY_LOADING_NOTE,
  HISTORY_LOAD_ERROR_NOTE,
  LOAD_MORE_ERROR_NOTE,
  LOAD_MORE_LABEL,
  LOAD_MORE_PENDING_LABEL,
  RETRY_LABEL,
} from '../historyCopy';
import { deleteHistoryEntryMutationKey } from '../hooks/historyKeys';
import { useDeleteHistoryEntry } from '../hooks/useDeleteHistoryEntry';
import { useHistory } from '../hooks/useHistory';

import type { HistoryEntryDeletion } from './HistoryEntryRow';
import type { HistoryEntry } from '../types';

export interface HistoryPageProps {
  /** The signed-in user — handed down by `AccountScope`, and the root of every history key. */
  readonly userId: string;
}

const BUTTON_CLASS =
  'min-h-11 rounded-lg bg-brand-600 px-4 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9';

/**
 * `/history` — a **container** (AC-44, AC-45), under `RequireAuth` and `AccountScope`.
 *
 * **Four states, each deliberate.** Loading (`role="status"`); error (`role="alert"` + Retry —
 * never the empty state, because "we could not ask" is not "you have nothing"); empty (*Nothing
 * tailored yet* + a link to the workspace); success (one `HistoryEntryRow` per entry).
 *
 * **Load more is a button, not infinite scroll** (AC-50), shown iff the server returned a
 * `next_cursor`. It has its own pending state, and a failed later page keeps every loaded row and
 * offers Retry for that page alone (H-59). `fetchNextPage({ cancelRefetch: false })` makes a second
 * click while a page is on the wire join it rather than cancel and re-ask.
 *
 * **Deleting** owns one piece of local state — which entry is being confirmed, an id and never a
 * copy of a row — and reads everything else from the mutation: `variables` says which row,
 * `isPending` says "Deleting…", `error` says why it was not deleted, a resolved `'already_gone'`
 * says the polite note. No optimistic removal: the row goes when the re-read list no longer has it.
 */
export function HistoryPage({ userId }: HistoryPageProps): React.JSX.Element {
  const headingId = useId();

  return (
    <section aria-labelledby={headingId} className="mb-10 space-y-4">
      <div className="space-y-1">
        <h2
          id={headingId}
          className="font-display text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl"
        >
          {HISTORY_HEADING}
        </h2>
        <p className="text-sm text-slate-600">{HISTORY_DELETION_NOTE}</p>
      </div>
      <HistoryBody userId={userId} />
    </section>
  );
}

function HistoryBody({ userId }: HistoryPageProps): React.JSX.Element {
  const history = useHistory(userId);
  const remove = useDeleteHistoryEntry(userId);
  const queryClient = useQueryClient();
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  // `data` is what separates "the first page failed" (nothing to show) from "a later page failed"
  // (TanStack keeps the pages it has and sets `isFetchNextPageError`).
  if (history.data === undefined) {
    if (history.isError) {
      return (
        <div
          role="alert"
          className="space-y-3 rounded-xl border border-danger-200 bg-danger-50 p-4"
        >
          <p className="text-sm font-medium text-slate-900">{HISTORY_LOAD_ERROR_NOTE}</p>
          <button
            type="button"
            onClick={() => {
              void history.refetch();
            }}
            className={BUTTON_CLASS}
          >
            {RETRY_LABEL}
          </button>
        </div>
      );
    }
    return (
      <p role="status" className="text-sm text-slate-600">
        {HISTORY_LOADING_NOTE}
      </p>
    );
  }

  const entries = history.data.pages.flatMap((page) => page.items);
  const alreadyGone = remove.isSuccess && remove.data === 'already_gone';

  if (entries.length === 0) {
    return (
      <div className="space-y-2">
        {alreadyGone && <AlreadyGoneNote />}
        <p className="rounded-xl border-2 border-dashed border-slate-300 bg-white p-6 text-sm text-slate-700">
          <span className="font-semibold text-slate-900">{HISTORY_EMPTY_NOTE}</span>{' '}
          <Link
            to="/"
            className="rounded-sm font-semibold text-brand-700 underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
          >
            {HISTORY_EMPTY_ACTION}
          </Link>
        </p>
      </div>
    );
  }

  // Validated against the list on every render: an entry that has gone has no dialog.
  const confirming = entries.find((entry) => entry.id === confirmingId);

  function deletionOf(entry: HistoryEntry): HistoryEntryDeletion {
    if (remove.variables !== entry.id) {
      return { kind: 'idle' };
    }
    if (remove.isPending) {
      return { kind: 'deleting' };
    }
    if (remove.isError) {
      const inProgress =
        remove.error instanceof ApiError && remove.error.code === 'tailoring_run_in_progress';
      return { kind: 'failed', reason: inProgress ? 'in_progress' : 'unavailable' };
    }
    return { kind: 'idle' };
  }

  function confirmDelete(id: string): void {
    setConfirmingId(null);
    // `isPending` lags a same-tick double click; the mutation cache does not (H-58, CLAUDE.md).
    if (queryClient.isMutating({ mutationKey: deleteHistoryEntryMutationKey }) > 0) {
      return;
    }
    remove.mutate(id);
  }

  return (
    <div className="space-y-4">
      {alreadyGone && <AlreadyGoneNote />}
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        {/* Visual only: each row says what its fields are, so the header is hidden from assistive
            technology rather than half-imitating a table (OQ-14). */}
        <div
          aria-hidden="true"
          className={`hidden border-b border-slate-200 bg-surface-sunk px-5 py-3 text-xs font-semibold tracking-wide text-slate-700 uppercase ${HISTORY_GRID_CLASS}`}
        >
          {HISTORY_COLUMN_LABELS.map((label) => (
            <span key={label}>{label}</span>
          ))}
          <span />
        </div>
        <ul className="divide-y divide-slate-200">
          {entries.map((entry) => (
            <HistoryEntryRow
              key={entry.id}
              entry={entry}
              deletion={deletionOf(entry)}
              onDelete={() => {
                setConfirmingId(entry.id);
              }}
            />
          ))}
        </ul>
      </div>

      {confirming !== undefined && (
        <DeleteHistoryEntryDialog
          onCancel={() => {
            setConfirmingId(null);
          }}
          onConfirm={() => {
            confirmDelete(confirming.id);
          }}
        />
      )}

      {history.isFetchNextPageError ? (
        <div
          role="alert"
          className="space-y-3 rounded-xl border border-danger-200 bg-danger-50 p-4"
        >
          <p className="text-sm font-medium text-slate-900">{LOAD_MORE_ERROR_NOTE}</p>
          <button
            type="button"
            onClick={() => {
              void history.fetchNextPage({ cancelRefetch: false });
            }}
            className={BUTTON_CLASS}
          >
            {RETRY_LABEL}
          </button>
        </div>
      ) : (
        history.hasNextPage && (
          <button
            type="button"
            onClick={() => {
              void history.fetchNextPage({ cancelRefetch: false });
            }}
            disabled={history.isFetchingNextPage}
            className={BUTTON_CLASS}
          >
            {history.isFetchingNextPage ? LOAD_MORE_PENDING_LABEL : LOAD_MORE_LABEL}
          </button>
        )
      )}
    </div>
  );
}

function AlreadyGoneNote(): React.JSX.Element {
  return (
    <p role="status" className="text-sm text-slate-600">
      {DELETE_ENTRY_ALREADY_GONE_NOTE}
    </p>
  );
}
