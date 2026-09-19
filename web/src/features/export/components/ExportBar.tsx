/**
 * The export bar — **the container**, and the only component in this feature that talks to the
 * network.
 *
 * It owns three things and derives everything else:
 *
 * 1. **The run's export jobs**, through `useExportJobs` — server state, in TanStack Query, polled
 *    while anything is in flight and never copied into `useState`. There is no `job`, no `view` and
 *    no `Blob` in this component's state; ask it what the PDF control shows and the honest answer
 *    is "whatever `viewOfExport` says about the cache right now".
 * 2. **Two mutations**, whose `variables` say *which* control is requesting or downloading. One
 *    `useRequestExport` and one `useDownload` sit behind four controls, so `isPending` alone would
 *    put all four into the same state at once.
 * 3. **One `useState`, ticked by one `setInterval`** — the wall clock, which is genuinely outside
 *    React and the one thing here a `useEffect` is for (1.3's `TailoringProgress` made the same
 *    call, and its docstring has the long version of the argument).
 *
 * ## The three bar-level notices, and why they are not per control
 *
 * *Checking your downloads…*, *We couldn't check your downloads.* and the save-gate reason are
 * rendered **once**, on the bar. They are statements about the whole bar — one list query, one save
 * state — and four copies of *Save your changes first — Saving…* beside four disabled buttons is
 * the same sentence shouted four times. The per-control `role="status"` regions stay for what is
 * genuinely per control: that format's own job.
 *
 * ## What the list error does and does not disable
 *
 * A failed list query disables `pdf` and `docx` only. **The two inline controls do not depend on
 * the list** (AC-43): Markdown and plain text are rendered inside the request and leave no job, so
 * there is nothing about them the list could have told us. Taking them down with it would be the
 * bar punishing the user for an outage in a part of the system their click does not touch.
 */

import { useEffect, useState } from 'react';

import { downloadDocument, downloadExportFile } from '@/api/exports';

import {
  EXPORT_LIST_ERROR_ACTION,
  EXPORT_LIST_ERROR_NOTE,
  EXPORT_LIST_LOADING_NOTE,
  EXPORT_PRIVACY_NOTE,
  downloadFilenameFor,
  exportGateReasonFor,
} from '../exportCopy';
import { viewOfExport } from '../exportView';
import { useDownload } from '../hooks/useDownload';
import { useExportJobs } from '../hooks/useExportJobs';
import { useRequestExport } from '../hooks/useRequestExport';
import { isActiveExportStatus } from '../types';
import { ExportControl } from './ExportControl';

import type { ExportMutations, ExportTarget, ExportView } from '../exportView';
import type { ExportJob, InlineExportFormat, QueuedExportFormat } from '../types';
import type { SaveState } from '@/features/editor/saveState';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

export interface ExportBarProps {
  /**
   * The run whose exports these are — **the id, not the run**.
   *
   * The plan writes `<ExportBar run={…} />` and its own state table then says the bar reads the run
   * "for nothing but the id (`current` comes from the server)". Taking the id makes that true by
   * construction. The one judgement a `TailoringRun` could contribute here is
   * `job.run_version === run.version`, which is the cross-aggregate comparison the API already made
   * and shipped as `ExportJob.current` — a `run` prop would be a standing invitation to re-derive
   * it in TypeScript (Constitution §4.5). `viewOfExport` dropped the same parameter, for the same
   * reason.
   */
  readonly runId: string;
  /**
   * Which document the four controls act on: the visible editor tab, which is the URL's
   * `:document` segment (1.4's rule — the visible tab is the URL, never a `useState`).
   *
   * Named as the wire and `ExportTarget` name it. Switching the tab switches which document's jobs
   * the controls reflect (AC-36): a PDF ready for the CV is not shown as ready for the letter.
   */
  readonly document: TailoredDocumentKind;
  /**
   * The **visible document's** autosave state, owned by 1.4's machine and passed down (F7 wires it
   * through `DocumentWorkspace`). The bar reads it and never holds it.
   *
   * It is the AC-40 gate: exports are offered only while the state is `saved`, because the server
   * renders the text *it* holds, and exporting a document the user is still typing into produces a
   * file that silently does not match the screen.
   */
  readonly saveState: SaveState;
}

/**
 * The four controls, in AC-36's order, each with the delivery that decides what its click does.
 *
 * **A discriminated union, not a `format` beside a loose `delivery` string.** Narrowing on
 * `delivery` narrows `format` with it, so the `md`/`txt` branch below can call `downloadDocument`
 * (whose parameter is `InlineExportFormat`) and the `pdf`/`docx` branch can call `requestExport`
 * (whose body types `QueuedExportFormat`) with no cast and no runtime check. The API's two endpoints
 * disagree about which formats they accept; this table is that disagreement, spelled once.
 */
type ExportControlSpec =
  | { readonly delivery: 'inline'; readonly format: InlineExportFormat }
  | { readonly delivery: 'queued'; readonly format: QueuedExportFormat };

const EXPORT_CONTROLS: readonly ExportControlSpec[] = [
  { delivery: 'inline', format: 'md' },
  { delivery: 'inline', format: 'txt' },
  { delivery: 'queued', format: 'pdf' },
  { delivery: 'queued', format: 'docx' },
];

/** A stable empty list, so that "no data yet" and "no jobs" are the same object every render. */
const NO_JOBS: readonly ExportJob[] = [];

const TICK_MS = 1000;

/**
 * The wall clock, re-read once a second while any job of this run is still working.
 *
 * **This is the one legitimate `useEffect` in the feature.** An effect synchronizes React with
 * something outside it; nothing re-renders when a second passes unless something subscribes to the
 * clock, so this subscribes, writes only local state, touches no server state and unsubscribes on
 * unmount. The poller next door does *not* qualify and does not have one: the job list is server
 * state, TanStack Query owns its timer, and a `setInterval` fetching would be a worse second copy.
 *
 * `Date.now()` is re-read on every tick rather than a counter being incremented, because browsers
 * throttle background intervals: a counter comes back from another tab claiming three seconds
 * passed after three minutes. The interval decides only *when* to look at the clock.
 *
 * **The subscription is conditional on there being something to count**, which is the difference
 * from 1.3's version. `TailoringProgress` is mounted only while a run is working, so an
 * unconditional interval was free; this bar is mounted for as long as the workspace is open, and an
 * unconditional interval would re-render it once a second for the entire session to animate a
 * number nobody is looking at. The clock is also re-read the moment the subscription starts, so a
 * job that was already running when the list arrived does not show a stale count for one second.
 */
function useNowMs(isCounting: boolean): number {
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (!isCounting) {
      return undefined;
    }
    setNowMs(Date.now());
    const timer = setInterval(() => {
      setNowMs(Date.now());
    }, TICK_MS);
    return () => {
      clearInterval(timer);
    };
  }, [isCounting]);

  return nowMs;
}

/**
 * Whether a control is unusable, and the three quite different reasons it can be.
 *
 * The last of them is AC-38, which is the distinction this whole slice exists to get right: a
 * *working* view offers **no control at all**. A user who reads *Preparing…* as *Failed* clicks
 * another format, and now two renders are queued and two workers are paid.
 */
function isControlDisabled(
  view: ExportView,
  spec: ExportControlSpec,
  { isGated, isListPending, isListError }: ExportBarGates,
): boolean {
  if (isGated || isListPending) {
    return true;
  }
  if (isListError && spec.delivery === 'queued') {
    return true;
  }
  switch (view.kind) {
    case 'idle':
    case 'ready':
    case 'stale':
      return false;
    // The four working states are AC-38 itself. `failed` and `downloadFailed` join them because
    // they put their own retry control *inside* the status region, next to the words explaining
    // what went wrong; a bare enabled button above them would offer the same click with none of the
    // explanation.
    case 'requesting':
    case 'queued':
    case 'rendering':
    case 'downloading':
    case 'failed':
    case 'downloadFailed':
    case 'requestFailed':
      return true;
  }
}

interface ExportBarGates {
  readonly isGated: boolean;
  readonly isListPending: boolean;
  readonly isListError: boolean;
}

export function ExportBar({ runId, document, saveState }: ExportBarProps): React.JSX.Element {
  const jobsQuery = useExportJobs(runId);
  const requestExport = useRequestExport(runId);
  const download = useDownload();

  const jobs = jobsQuery.data?.items ?? NO_JOBS;
  const nowMs = useNowMs(jobs.some((job) => isActiveExportStatus(job.status)));
  // When this bar first appeared, so a control can tell "this job is old" from "this user has been
  // waiting". Lazy `useState` rather than `useRef`, because it is read during render and never
  // written again — a constant for the lifetime of the mount, which is exactly what it means.
  const [mountedAtMs] = useState(() => Date.now());
  const secondsOnPage = Math.max(0, Math.floor((nowMs - mountedAtMs) / 1000));

  // What this browser's own two requests are doing, narrowed to what the derivation needs. Read
  // from the mutations' `variables` — the record of what was asked for — rather than from a
  // `useState` holding the pending format, which would be a second copy of a fact TanStack holds.
  const requestVariables = requestExport.variables;
  const downloadVariables = download.variables;
  const mutations: ExportMutations = {
    requesting:
      requestExport.isPending && requestVariables !== undefined
        ? { document: requestVariables.document, format: requestVariables.format }
        : null,
    downloading:
      download.isPending && downloadVariables !== undefined
        ? { document: downloadVariables.document, format: downloadVariables.format }
        : null,
    downloadFailure:
      download.isError && downloadVariables !== undefined
        ? {
            target: { document: downloadVariables.document, format: downloadVariables.format },
            error: download.error,
          }
        : null,
    // The exact twin of `downloadFailure` above, and its absence was slice 1.5's second `/verify`
    // finding: `requestExport.isError` was read nowhere, so five refusals of `POST /exports` showed
    // the user nothing whatever. Three of them commit no row, so the poll could never say it either.
    requestFailure:
      requestExport.isError && requestVariables !== undefined
        ? {
            target: { document: requestVariables.document, format: requestVariables.format },
            error: requestExport.error,
          }
        : null,
  };

  const gateReason = exportGateReasonFor(saveState);
  const gates: ExportBarGates = {
    isGated: gateReason !== null,
    isListPending: jobsQuery.isPending,
    isListError: jobsQuery.isError,
  };

  /**
   * What one control's click means — **decided from the view, not from the job row**.
   *
   * It used to read the row (`status === 'ready' && current` → download, otherwise request), and
   * that is the bug `/verify` found in slice 1.5. A 410 `export_file_gone` does not change the row:
   * the download endpoint refuses and writes nothing, which is correct, so the row goes on saying
   * *ready* while the file it names is gone. The control therefore stayed wired to the identical
   * `GET` for ever — copy that read *"That file is no longer available — Export again"* above a
   * button that 410'd again on every press, and a reload that came back to *Download PDF* → 410.
   *
   * The view already knows all of it, because `viewOfExport` saw the rejection: it is the one place
   * that holds both what the server last said *and* what this browser's own request just found out.
   * Reading it here also deletes the second derivation — the row was being interrogated twice, once
   * for what to render and once for what a click does, and those two answers are exactly what
   * disagreed.
   *
   * An inline format always downloads: there is nothing to queue, and no job for a 404/410 to be
   * about.
   */
  function primaryActionFor(spec: ExportControlSpec, view: ExportView): () => void {
    if (spec.delivery === 'inline') {
      const format = spec.format;
      return () => {
        download.mutate({
          document,
          format,
          filename: downloadFilenameFor(document, format),
          fetchBlob: () => downloadDocument(runId, document, format),
        });
      };
    }

    const format = spec.format;

    /** Pay a worker for a fresh render of this (document, format). */
    function requestAgain(): void {
      requestExport.mutate({ document, format });
    }

    /** Fetch one export job's bytes and save them under this format's constant filename. */
    function downloadJob(jobId: string): () => void {
      return () => {
        download.mutate({
          document,
          format,
          filename: downloadFilenameFor(document, format),
          fetchBlob: () => downloadExportFile(jobId),
        });
      };
    }

    switch (view.kind) {
      case 'ready':
        // The one state that offers the file, and the jobId comes from the view rather than a
        // second lookup — `viewOfExport` chose this member *from* that row.
        return downloadJob(view.jobId);
      case 'downloadFailed':
        if (view.nextAction === 'download') {
          // 401, 5xx, a dropped connection: nothing was said about the file, so *Try again* repeats
          // the request that failed. `download.variables` **is** that request — the same thunk over
          // the same job id — which is why nothing is rebuilt here from `jobs`.
          const lastDownload = download.variables;
          if (lastDownload !== undefined) {
            return () => {
              download.mutate(lastDownload);
            };
          }
        }
        // 410 / 404, and the unreachable case above: the file or its job is gone, and repeating the
        // same `GET` can only fail the same way. Only a new export can produce something to
        // download, which is what the copy already promises the user.
        //
        // The rejection is cleared first, and it must be: `downloadFailed` outranks the job in
        // `viewOfExport` (it is a fact about this browser, which the server does not know yet), so
        // leaving it on the mutation would keep *"That file is no longer available"* on screen over
        // the render this click just paid for. It is scoped to this branch rather than to every
        // request, because one `useDownload` sits behind four controls and resetting it from a
        // different control would wipe a failure notice the user has not answered.
        return () => {
          download.reset();
          requestAgain();
        };
      case 'requestFailed':
        // The refusal must be cleared before asking again, for `downloadFailed`'s reason one branch
        // up: `requestFailed` outranks the job in `viewOfExport`, so a stale rejection would sit on
        // top of the very request this click is making. `reset()` on the *request* mutation, not the
        // download one — they are two mutations and two failures, and clearing the wrong one would
        // both leave this sentence up and silently drop a download error elsewhere on the bar.
        return () => {
          requestExport.reset();
          requestAgain();
        };
      case 'idle':
      case 'requesting':
      case 'queued':
      case 'rendering':
      case 'stale':
      case 'downloading':
      case 'failed':
        // *stale* requests rather than downloads on purpose: the old file is still on the server,
        // but what the user asked for by clicking a control labelled *Export again* is a new one.
        // The four working states are unclickable anyway (`isControlDisabled`); they are listed so
        // that a tenth view member is a compile error here instead of a silent `default`.
        return requestAgain;
    }
  }

  return (
    <section
      aria-label="Download this document"
      className="space-y-3 rounded-md border border-slate-200 bg-white px-4 py-3"
    >
      <div className="flex flex-wrap gap-4">
        {EXPORT_CONTROLS.map((spec) => {
          const target: ExportTarget = { document, format: spec.format };
          const view = viewOfExport(target, jobs, mutations, nowMs);
          return (
            <ExportControl
              key={spec.format}
              format={spec.format}
              view={view}
              disabled={isControlDisabled(view, spec, gates)}
              secondsOnPage={secondsOnPage}
              onPrimary={primaryActionFor(spec, view)}
            />
          );
        })}
      </div>

      {/*
        Neither bar-level line is a `role="status"`, and that is the amendment's point rather than an
        oversight: AC-36 asks for **four** live regions, one per control, and a fifth that comes and
        goes would have a screen reader announce the bar's own bookkeeping alongside the four states
        a user actually asked about. The gate reason is on screen before any control can be clicked,
        and the loading line describes a render that is about to be replaced.
      */}
      {gateReason !== null && <p className="text-sm text-slate-700">{gateReason}</p>}

      {gateReason === null && jobsQuery.isPending && (
        <p className="text-sm text-slate-600">{EXPORT_LIST_LOADING_NOTE}</p>
      )}

      {gateReason === null && jobsQuery.isError && (
        // `role="alert"`, not `status`: this one is assertive because the user is about to click a
        // control that cannot work, and it carries the only thing that can fix it. It is also a
        // different role, so the four per-control live regions stay four.
        <div role="alert" className="text-sm text-rose-800">
          {EXPORT_LIST_ERROR_NOTE}{' '}
          <button
            type="button"
            onClick={() => {
              // The rejection is already the query's error state — it is rendered right here — so
              // there is nothing for a `.catch` to do, and an unhandled rejection would be noise in
              // the console for a failure the UI has fully accounted for.
              void jobsQuery.refetch();
            }}
            className="rounded-md px-1.5 py-0.5 font-medium underline underline-offset-2 hover:bg-rose-100"
          >
            {EXPORT_LIST_ERROR_ACTION}
          </button>
        </div>
      )}

      <p className="text-xs text-slate-500">{EXPORT_PRIVACY_NOTE}</p>
    </section>
  );
}
