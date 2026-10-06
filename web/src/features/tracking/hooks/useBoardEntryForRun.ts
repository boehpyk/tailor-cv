import { useQuery } from '@tanstack/react-query';
import { useCallback } from 'react';

import { boardQueryOptions } from './boardCache';

import type { Board, BoardCard } from '../types';

/**
 * The board's card for one run, or `null` — a `select` over the **same** board query (plan §7), so a
 * history row's *"On your board · {stage}"* badge needs no new history field (AC-38, AC-41), and a
 * page of twenty rows reads the board once.
 *
 * `null` means "not on the board" **or** "the board is not loaded (yet, or it failed)". Both render
 * *Add to board*: a click on a run that is in fact tracked answers 409 `application_already_tracked`,
 * which `useTrackApplication` treats as success.
 */
export function useBoardEntryForRun(userId: string, runId: string): BoardCard | null {
  const select = useCallback(
    (board: Board): BoardCard | null =>
      board.items.find((card) => card.tailoring_run_id === runId) ?? null,
    [runId],
  );
  const query = useQuery({ ...boardQueryOptions(userId), select });
  return query.data ?? null;
}
