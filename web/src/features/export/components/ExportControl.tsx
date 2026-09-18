/**
 * One format's control: a `<button>` and a `role="status"` line, rendered from one `ExportView`.
 *
 * **Presentational.** It calls no hook, holds no state, knows no run id and never asks what a job
 * is — it is handed a view and a callback and renders them. Everything that could be wrong about
 * *which* view this is was decided in `viewOfExport`, which is a pure function with a table test;
 * everything that could be wrong about *what a click does* was decided in `ExportBar`, which is the
 * only component here that talks to the network.
 *
 * ## One live region per control, whatever the delivery (AC-36, amended 2026-09-17)
 *
 * The F4 skeleton rendered a `role="status"` only for `pdf` and `docx`, which was AC-36's original
 * wording. It is wrong: an inline format has three states of its own (`idle`, `downloading`,
 * `downloadFailed`) and AC-42 requires *Couldn't download* + *Try again* to appear on that control.
 * Copy that appears with no live region is worse for a screen-reader user than a redundant empty
 * one — an empty `role="status"` announces nothing at all — so every control gets one and this
 * component no longer takes a `delivery` prop, because that prop's only job was to split them.
 *
 * ## One action button, not two
 *
 * `onPrimary` is the single thing this control does, and the caller decides what that is from the
 * job list (download the file, or pay for a new render). *Export again* on a failed job and *Try
 * again* on a failed download are the same callback under different words, because in both cases
 * the right thing to do is exactly what the button above would have done. A second callback would
 * be a second chance for the two to disagree.
 */

import {
  DOWNLOAD_AGAIN_ACTION,
  EXPORT_AGAIN_ACTION,
  EXPORT_DOWNLOADING_NOTE,
  EXPORT_FORMAT_LABELS,
  EXPORT_READY_NOTE,
  EXPORT_REQUESTING_NOTE,
  EXPORT_STALE_NOTE,
  EXPORT_STILL_WORKING_NOTE,
  EXPORT_WAITING_NOTE,
  STILL_WORKING_AFTER_SECONDS,
  downloadLabel,
  preparingNote,
} from '../exportCopy';
import { ExportFailureNotice } from './ExportFailureNotice';

import type { ExportView } from '../exportView';
import type { ExportFormat } from '../types';
import type { ReactNode } from 'react';

export interface ExportControlProps {
  readonly format: ExportFormat;
  /** This format's state, derived by `viewOfExport` from the run's jobs and the bar's mutations. */
  readonly view: ExportView;
  /**
   * Whether the button is unusable — the save gate (AC-40), the list still loading or in error
   * (AC-43), or a view in which there is simply nothing to click (AC-38: **a working state offers
   * no control at all**, so that nobody reads *Preparing…* as *Failed* and queues a second render).
   * Computed by the bar, because three of those four reasons are the bar's to know.
   */
  readonly disabled: boolean;
  /**
   * How long **this page** has been open, in whole seconds — the clock behind AC-37's 20-second
   * *"This is taking longer than usual"* line, and deliberately not the job's own age.
   *
   * 1.3's `TailoringProgress` wrote the reason down and this is the same call: the job's
   * `requested_at` is a *different machine's* timestamp, and a control that reads it alone opens on
   * *"taking longer than usual"* for a job the user has been watching for half a second — after a
   * refresh, or when a clock is a minute out. The two are combined below with `Math.min`, so an
   * export started ten minutes into the session still gets its full twenty seconds.
   */
  readonly secondsOnPage: number;
  /** Export this format, or download it — whichever the bar decided this control's click means. */
  readonly onPrimary: () => void;
}

/**
 * Whether the working copy has been on screen long enough to be replaced by the 20-second line.
 *
 * `Math.min` of the job's age and the page's: the notice is a claim about **this user's wait**, and
 * neither number alone is that. The job's age alone announces an old job as slow the instant the
 * page opens; the page's age alone announces a brand-new export as slow because the tab happened to
 * be open a while.
 */
function isTakingTooLong(elapsedSeconds: number, secondsOnPage: number): boolean {
  return Math.min(elapsedSeconds, secondsOnPage) >= STILL_WORKING_AFTER_SECONDS;
}

/**
 * The button's words. Only two states rename it: `ready` offers the file and its size, and `stale`
 * offers a fresh render. Every other state leaves the plain format label in place, which is what
 * keeps the four controls readable as a row of formats rather than a row of sentences.
 *
 * Written as a `switch` over every member rather than a `default`, so a tenth view state is a
 * compile error here instead of a button that silently says "PDF" in a state nobody considered.
 */
function primaryLabelFor(view: ExportView, format: ExportFormat): string {
  switch (view.kind) {
    case 'ready':
      return downloadLabel(format, view.byteSize);
    case 'stale':
      return EXPORT_AGAIN_ACTION;
    case 'idle':
    case 'requesting':
    case 'queued':
    case 'rendering':
    case 'downloading':
    case 'failed':
    case 'downloadFailed':
      return EXPORT_FORMAT_LABELS[format];
  }
}

/**
 * What goes in the live region.
 *
 * `null` only for `idle`, where there is nothing to announce. `ready` deliberately says so in words
 * rather than letting the button quietly rename itself: a screen-reader user who has been waiting
 * twenty seconds for a PDF is told it arrived, which is the whole point of a live region.
 *
 * The 20-second line **replaces** the working copy rather than joining it (AC-37): two sentences in
 * a polite region that re-announces every second is noise, and the newer one carries the
 * information.
 */
function statusContentFor(
  view: ExportView,
  format: ExportFormat,
  secondsOnPage: number,
  onPrimary: () => void,
): ReactNode {
  switch (view.kind) {
    case 'idle':
      return null;
    case 'requesting':
      return EXPORT_REQUESTING_NOTE;
    case 'queued':
      return isTakingTooLong(view.elapsedSeconds, secondsOnPage)
        ? EXPORT_STILL_WORKING_NOTE
        : EXPORT_WAITING_NOTE;
    case 'rendering':
      return isTakingTooLong(view.elapsedSeconds, secondsOnPage)
        ? EXPORT_STILL_WORKING_NOTE
        : // The *count* is the job's own age, not the page's: it is a fact about the render, and a
          // reader who refreshes should not see it restart at zero. Only the judgement above is the
          // page's.
          preparingNote(format, view.elapsedSeconds);
    case 'ready':
      return EXPORT_READY_NOTE;
    case 'stale':
      return EXPORT_STALE_NOTE;
    case 'downloading':
      return EXPORT_DOWNLOADING_NOTE;
    case 'failed':
      return (
        <ExportFailureNotice
          reason={view.reason}
          format={format}
          retryable={view.retryable}
          onRetry={onPrimary}
        />
      );
    case 'downloadFailed':
      return (
        <>
          {view.message}
          <button
            type="button"
            onClick={onPrimary}
            className="ml-2 rounded-md px-1.5 py-0.5 font-medium text-rose-800 underline underline-offset-2 hover:bg-rose-100"
          >
            {DOWNLOAD_AGAIN_ACTION}
          </button>
        </>
      );
  }
}

export function ExportControl({
  format,
  view,
  disabled,
  secondsOnPage,
  onPrimary,
}: ExportControlProps): React.JSX.Element {
  return (
    <div className="flex min-w-36 flex-col gap-1">
      <button
        type="button"
        disabled={disabled}
        onClick={onPrimary}
        className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {primaryLabelFor(view, format)}
      </button>
      {/*
        A `<div>` rather than a `<p>`: two of the nine states put a button in this region, and a
        `<button>` inside a `<p>` is legal but makes the spacing fight the paragraph's line box.
        `role="status"` is a polite live region — announced when it changes, never interrupting.
      */}
      <div role="status" className="text-xs leading-5 text-slate-600">
        {statusContentFor(view, format, secondsOnPage, onPrimary)}
      </div>
    </div>
  );
}
