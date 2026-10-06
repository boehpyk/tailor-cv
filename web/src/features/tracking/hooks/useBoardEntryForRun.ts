/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the parameters are the signatures qa's T27 tests compile against; T28 uses them and deletes this line. */
import type { BoardCard } from '../types';

/**
 * The board's card for one run, or `null` — a `select` over the **same** board query (plan §7), so a
 * history row's *"On your board · {stage}"* badge needs no new history field (AC-38, AC-41).
 *
 * `null` means "not on the board" **or** "the board is not loaded (yet, or it failed)". Both render
 * *Add to board*: a click on a run that is in fact tracked answers 409 `application_already_tracked`,
 * which `useTrackApplication` treats as success.
 *
 * SKELETON (T26): no query; always `null`. T28 implements it.
 */
export function useBoardEntryForRun(_userId: string, _runId: string): BoardCard | null {
  return null;
}
