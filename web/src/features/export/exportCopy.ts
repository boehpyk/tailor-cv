/**
 * The words the export bar says, kept out of the components that say them.
 *
 * A string a test pins (AC-35) and a string a component happens to contain are different things,
 * and the difference shows up when someone reflows the JSX. `features/tailoring/failureCopy.ts` made
 * the same call for the same reason: copy that a specification states verbatim lives in a module
 * whose whole content is that copy.
 *
 * Two of the maps below are **exhaustive by their type** — one over `ExportFailureReason`, one over
 * `SaveState['kind']` — so a tenth failure reason on the server, or a ninth save state in the
 * editor, is a TypeScript error here (at the one place that must write a sentence for it) rather
 * than a blank box in somebody's browser. The third, the download-failure map, cannot be: its keys
 * are the API's error `code`s, an open set, so it is a lookup with a fallback sentence and that
 * fallback is the thing the tests pin.
 */

import { ApiError } from '@/api/client';
import { saveStateCopy } from '@/features/editor/saveState';
// Reused rather than re-written. `formatSize` is a pure formatter with no intake in it — it was
// split out of a presentational component in 1.1 for exactly this kind of reuse — and a second
// implementation here would be a second answer to "is 86,016 bytes 84 KB or 86 KB", which is the
// sort of disagreement nobody notices until two screens show different numbers for one file.
import { formatSize } from '@/features/intake/format';

import type { ExportFailureReason, ExportFormat } from './types';
import type { SaveState } from '@/features/editor/saveState';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

/**
 * What each format is called on its control — and note that none of them is the wire spelling.
 *
 * *Word* rather than *DOCX*, because a job seeker in a hurry knows what Word is and may not know
 * what a DOCX is; *Plain text* rather than *TXT* for the same reason. *Markdown* and *PDF* are
 * already the names people use. The `Record<ExportFormat, string>` is exhaustive by type, so a fifth
 * format cannot ship with an unlabelled button.
 */
export const EXPORT_FORMAT_LABELS: Record<ExportFormat, string> = {
  md: 'Markdown',
  txt: 'Plain text',
  pdf: 'PDF',
  docx: 'Word',
};

/**
 * The same four formats as the object of a sentence — *"…into **a PDF**."*
 *
 * A second map rather than an article glued onto `EXPORT_FORMAT_LABELS`, because English does not
 * cooperate: *"a Word"* is not a thing, and *"a Markdown"* is not either. The noun phrase is the
 * unit that varies, so the noun phrase is what is written down.
 */
const EXPORT_FORMAT_NOUNS: Record<ExportFormat, string> = {
  md: 'a Markdown file',
  txt: 'a plain-text file',
  pdf: 'a PDF',
  docx: 'a Word document',
};

/**
 * AC-35, verbatim — where the file is made, how long it lives, and who else sees it.
 *
 * **This sentence is the export bar's half of the privacy promise** (Constitution §8, ADR-0006),
 * and it is on the bar rather than in a policy page nobody opens, exactly as 1.3 put the Gemini
 * disclosure next to the button that sends the CV. Each clause is a fact about this system and not
 * a reassurance: the render happens in our worker (no third party renders the PDF), the file is
 * deleted with the guest session inside 24 hours (a scheduled job enforces it), and nothing about
 * the document leaves this server in the process.
 *
 * Exported as a constant because a Vitest assertion pins it. Change the wording here and the test
 * fails, which is the intended cost — a sentence that promises a retention window should not be
 * edited by accident.
 */
export const EXPORT_PRIVACY_NOTE =
  'Your files are made on our server, kept for 24 hours, and never sent anywhere else.';

/** AC-43's loading line, on the bar and not on each control (see `ExportBar`'s docstring). */
export const EXPORT_LIST_LOADING_NOTE = 'Checking your downloads…';

/** AC-43's list-error line, and the control that answers it. */
export const EXPORT_LIST_ERROR_NOTE = "We couldn't check your downloads.";
export const EXPORT_LIST_ERROR_ACTION = 'Check again';

/** AC-37's two working sentences, and the one that replaces them after 20 seconds. */
export const EXPORT_WAITING_NOTE = 'Waiting for a worker…';
export const EXPORT_STILL_WORKING_NOTE = 'This is taking longer than usual — we are still working.';

/** How long a job may sit in `queued`/`rendering` before the copy says so (AC-37). */
export const STILL_WORKING_AFTER_SECONDS = 20;

/**
 * *Preparing your PDF… 3s* — **one string, not a sentence with a `<span>` of seconds inside it.**
 *
 * 1.3's `TailoringProgress` puts the ticking count in an `aria-hidden` span so a screen reader is
 * not told the number every second. That is the better a11y default and it is deliberately not
 * copied here: AC-37 pins this line as one contiguous string, and a nested element would split the
 * text node in two, so the words and the count must share one node. The live region is `polite`, so
 * the announcements queue behind whatever the user is doing rather than interrupting.
 */
export function preparingNote(format: ExportFormat, elapsedSeconds: number): string {
  return `Preparing your ${EXPORT_FORMAT_LABELS[format]}… ${String(elapsedSeconds)}s`;
}

/** AC-37's stale sentence: the file is real, it is simply of a version the user has since edited. */
export const EXPORT_STALE_NOTE = 'Your document changed — Export again';

/** The two retry controls. *Export again* pays a worker; *Try again* re-fetches bytes that exist. */
export const EXPORT_AGAIN_ACTION = 'Export again';
export const DOWNLOAD_AGAIN_ACTION = 'Try again';

/** The three client-side working lines. None of them shares a substring with a failure line (AC-38). */
export const EXPORT_REQUESTING_NOTE = 'Starting…';
export const EXPORT_DOWNLOADING_NOTE = 'Downloading…';
export const EXPORT_READY_NOTE = 'Ready to download.';

/**
 * A ready control's label — *Download PDF · 84 KB*, or *Download PDF* when the size is unknown.
 *
 * **The `null` branch is not defensive padding.** `byte_size` is nullable on every
 * `ExportJobResponse`, because one Pydantic model serves all four statuses; the database's `CHECK`
 * does guarantee it on a `ready` row, and asserting that guarantee here with a `!` would make this
 * client a second authority on a server-side invariant (Constitution §4.5) whose failure mode is
 * *NaN KB* in a stranger's browser. The bytes are offered either way, because a download does not
 * depend on knowing how many of them there are.
 */
export function downloadLabel(format: ExportFormat, byteSize: number | null): string {
  const label = `Download ${EXPORT_FORMAT_LABELS[format]}`;
  return byteSize === null ? label : `${label} · ${formatSize(byteSize)}`;
}

/**
 * `failure_reason` → the sentence a failed control shows. **Exhaustive over the union by type.**
 *
 * Every entry is a function of the format's noun phrase even where it does not use one, so that the
 * map has one shape: a `string | ((noun: string) => string)` union would push a `typeof` check into
 * every reader for the sake of saving eight characters here.
 *
 * **What this map does not decide is whether *Export again* is offered.** That is the job's
 * `retryable`, computed by the API from the same reason (AC-24). `failureCopy.ts` carries the same
 * note one context over, for the same reason: a copy of a business rule in the copy layer is the
 * copy that goes stale.
 */
const EXPORT_FAILURE_COPY: Readonly<Record<ExportFailureReason, (noun: string) => string>> = {
  render_failed: (noun) => `We couldn't turn this document into ${noun}. Edit it and try again.`,
  render_timed_out: () => 'That took too long.',
  output_too_large: () => 'That file came out too large to send.',
  file_store_unavailable: () => "We couldn't store the finished file.",
  source_changed: () => 'Your document changed while we were making that file.',
  source_unavailable: () => "We couldn't read that document.",
  not_queued: () => 'That export never reached a worker.',
  abandoned: () => 'That export was interrupted.',
  render_error: () => 'Something went wrong making that file.',
};

/**
 * The sentence for a `null` reason. Unreachable in practice — the row's `CHECK` requires a reason
 * on a `failed` job — and handled anyway, because an unreachable branch asserted away with `!` is
 * unreachable only until someone adds a fifth status. `failureCopyFor`'s precedent, literally.
 */
const UNKNOWN_EXPORT_FAILURE_COPY = 'Something went wrong making that file.';

export function exportFailureCopyFor(
  reason: ExportFailureReason | null,
  format: ExportFormat,
): string {
  return reason === null
    ? UNKNOWN_EXPORT_FAILURE_COPY
    : EXPORT_FAILURE_COPY[reason](EXPORT_FORMAT_NOUNS[format]);
}

/** AC-42's generic download failure — the one a 5xx, a network drop or an unknown code lands on. */
export const DOWNLOAD_FAILED_NOTE = "Couldn't download";

/**
 * The API's error `code` → the sentence that control shows (AC-42).
 *
 * Keyed on `code` and not on `message`: `code` is the contract, `message` is prose for a human and
 * can be reworded without notice. The status is only a fallback, for a proxy that answered with no
 * envelope at all.
 */
const DOWNLOAD_FAILURE_COPY: Readonly<Partial<Record<string, string>>> = {
  guest_session_expired: 'Your session has expired',
  export_job_not_found: "We couldn't find that file",
  export_file_gone: 'That file is no longer available — Export again',
};

/** The same three, reachable when the body was not the error envelope (`code` is then `null`). */
const DOWNLOAD_FAILURE_COPY_BY_STATUS: Readonly<Partial<Record<number, string>>> = {
  401: 'Your session has expired',
  404: "We couldn't find that file",
  410: 'That file is no longer available — Export again',
};

/**
 * Turn a rejected download into the sentence its control shows — or `null` when the control should
 * show **nothing of its own** and fall back to whatever the poller says the job is.
 *
 * That `null` is AC-42's 409 `export_not_ready` row, and it is the interesting one. It means the
 * click lost a race: the file was ready when the button was drawn and is not ready now (the run was
 * edited, the job re-requested). There is no useful sentence for that — the job's own polled state
 * *is* the answer, and it is already on its way — so the failure is dropped rather than rendered.
 * Any other outcome would be the client inventing an error out of a stale click.
 */
export function downloadFailureCopyFor(error: Error): string | null {
  if (!(error instanceof ApiError)) {
    return DOWNLOAD_FAILED_NOTE;
  }
  if (error.code === 'export_not_ready') {
    return null;
  }
  return (
    (error.code === null ? undefined : DOWNLOAD_FAILURE_COPY[error.code]) ??
    DOWNLOAD_FAILURE_COPY_BY_STATUS[error.status] ??
    DOWNLOAD_FAILED_NOTE
  );
}

/**
 * Why the four controls are disabled, or `null` when they are not (AC-40).
 *
 * **Exhaustive over `SaveState['kind']`**, so 1.4's machine cannot grow a ninth state without this
 * line refusing to compile. Seven of the eight share one prefix and borrow their second half from
 * `saveStateCopy`, which is the indicator's own wording — the bar and the indicator saying two
 * different things about one save is precisely the confusion AC-40 exists to prevent.
 *
 * `expired` is the exception and says only that the session is gone: *"Save your changes first"* is
 * advice the user cannot take, because there is nothing left to save into.
 */
const EXPORT_GATE_REASONS: Readonly<Record<SaveState['kind'], string | null>> = {
  saved: null,
  dirty: `Save your changes first — ${saveStateCopy.dirty}`,
  saving: `Save your changes first — ${saveStateCopy.saving}`,
  failed: `Save your changes first — ${saveStateCopy.failed}`,
  conflict: `Save your changes first — ${saveStateCopy.conflict}`,
  paused: `Save your changes first — ${saveStateCopy.paused}`,
  invalid: `Save your changes first — ${saveStateCopy.invalid}`,
  expired: 'Your session has expired',
};

export function exportGateReasonFor(saveState: SaveState): string | null {
  return EXPORT_GATE_REASONS[saveState.kind];
}

/**
 * The name a downloaded file lands under — **eight constants, matching the server's
 * `download_filename` exactly** (AC-27).
 *
 * Two declarations of one contract, which is the same seam `types.ts` documents for the wire types,
 * and it is deliberate here rather than parsed out of `Content-Disposition`: `requestBlob` does not
 * read that header, because parsing a header the server controls in order to name a file on the
 * user's disk is a small attack surface for no gain when the value is a constant on both sides.
 * Nothing in either name is user text.
 */
const DOWNLOAD_FILENAMES: Readonly<
  Record<TailoredDocumentKind, Readonly<Record<ExportFormat, string>>>
> = {
  cv: {
    md: 'tailored-cv.md',
    txt: 'tailored-cv.txt',
    pdf: 'tailored-cv.pdf',
    docx: 'tailored-cv.docx',
  },
  cover_letter: {
    md: 'cover-letter.md',
    txt: 'cover-letter.txt',
    pdf: 'cover-letter.pdf',
    docx: 'cover-letter.docx',
  },
};

export function downloadFilenameFor(document: TailoredDocumentKind, format: ExportFormat): string {
  return DOWNLOAD_FILENAMES[document][format];
}
