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
 * are the API's error `code`s, an open set, so it is a lookup with a fallback entry and that
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
 * What the *Try again* beside a failed download has to do to have a chance of working.
 *
 * - `'download'` — **fetch the same bytes again.** The file, as far as anyone here knows, is still
 *   on the server: a 5xx, a dropped connection, a 401 whose session the user can renew. Repeating a
 *   `GET` is cheap and idempotent, and paying a worker for a fresh render would be spending money to
 *   fix a problem that was never about the file.
 * - `'request'` — **ask for a new export.** The file or the job it belonged to is gone (410 / 404).
 *   The `GET` that just failed can only fail the same way for the same reason, so a retry wired to
 *   it is a button that reproduces its own error. Only a new render can produce something to
 *   download.
 */
export type ExportNextAction = 'download' | 'request';

/**
 * A rejected download, as the control shows it: the sentence, **and what the retry beside it does**.
 *
 * The two travel together deliberately. Before this, the sentence lived here and the action was
 * re-derived in `ExportBar` from the job row — and they disagreed exactly where it mattered: a 410
 * leaves the row saying `ready`, so the copy read *"That file is no longer available — Export
 * again"* over a button that re-issued the identical `GET` that had just 410'd (`/verify` slice 1.5,
 * MAJOR 2). One record per failure makes that disagreement unspellable: the words and the recovery
 * they promise are one literal.
 */
export interface DownloadFailureView {
  readonly message: string;
  readonly nextAction: ExportNextAction;
}

/**
 * The three named failures, written once and looked up two ways below.
 *
 * `export_file_gone` and `export_job_not_found` are the `'request'` pair, and note that their copy
 * says so — *Export again* is in the 410's sentence. That is the sentence promising the action, so
 * the action is in the same object as the sentence.
 */
const SESSION_EXPIRED_FAILURE: DownloadFailureView = {
  message: 'Your session has expired',
  nextAction: 'download',
};
const JOB_NOT_FOUND_FAILURE: DownloadFailureView = {
  message: "We couldn't find that file",
  nextAction: 'request',
};
const FILE_GONE_FAILURE: DownloadFailureView = {
  message: 'That file is no longer available — Export again',
  nextAction: 'request',
};

/**
 * The API's error `code` → what that control shows and offers (AC-42).
 *
 * Keyed on `code` and not on `message`: `code` is the contract, `message` is prose for a human and
 * can be reworded without notice. The status is only a fallback, for a proxy that answered with no
 * envelope at all.
 */
const DOWNLOAD_FAILURE_BY_CODE: Readonly<Partial<Record<string, DownloadFailureView>>> = {
  guest_session_expired: SESSION_EXPIRED_FAILURE,
  export_job_not_found: JOB_NOT_FOUND_FAILURE,
  export_file_gone: FILE_GONE_FAILURE,
};

/**
 * The same three, reachable when the body was not the error envelope (`code` is then `null`).
 *
 * They carry the same `nextAction` as their coded twins, which is the point of sharing the objects
 * rather than repeating the literals: a 410 that arrives from a proxy with no JSON envelope is still
 * a file that is gone, and a retry that downloaded instead would 410 again just as surely.
 */
const DOWNLOAD_FAILURE_BY_STATUS: Readonly<Partial<Record<number, DownloadFailureView>>> = {
  401: SESSION_EXPIRED_FAILURE,
  404: JOB_NOT_FOUND_FAILURE,
  410: FILE_GONE_FAILURE,
};

/** Anything unrecognised: say so plainly, and let the retry repeat the download (see above). */
const UNKNOWN_DOWNLOAD_FAILURE: DownloadFailureView = {
  message: DOWNLOAD_FAILED_NOTE,
  nextAction: 'download',
};

/**
 * Turn a rejected download into the sentence its control shows and the action its retry takes — or
 * `null` when the control should show **nothing of its own** and fall back to whatever the poller
 * says the job is.
 *
 * That `null` is AC-42's 409 `export_not_ready` row, and it is the interesting one. It means the
 * click lost a race: the file was ready when the button was drawn and is not ready now (the run was
 * edited, the job re-requested). There is no useful sentence for that — the job's own polled state
 * *is* the answer, and it is already on its way — so the failure is dropped rather than rendered.
 * Any other outcome would be the client inventing an error out of a stale click.
 */
export function downloadFailureFor(error: Error): DownloadFailureView | null {
  if (!(error instanceof ApiError)) {
    // A `TypeError` from `fetch` — the network went away mid-request. Nothing was said about the
    // file, so the retry asks for it again.
    return UNKNOWN_DOWNLOAD_FAILURE;
  }
  if (error.code === 'export_not_ready') {
    return null;
  }
  return (
    (error.code === null ? undefined : DOWNLOAD_FAILURE_BY_CODE[error.code]) ??
    DOWNLOAD_FAILURE_BY_STATUS[error.status] ??
    UNKNOWN_DOWNLOAD_FAILURE
  );
}

/**
 * A rejected `POST /exports`, as the control shows it: the sentence, and whether a retry can help.
 *
 * Sibling of `DownloadFailureView` and deliberately **not** the same type. A download's retry has
 * two genuinely different meanings (fetch the same bytes, or render new ones), which is why that one
 * carries an `ExportNextAction`; a request's retry has only ever one meaning — ask again — so the
 * question here is the simpler `retryable`: *is asking again capable of succeeding?*
 */
export interface RequestFailureView {
  readonly message: string;
  readonly retryable: boolean;
}

/**
 * The API's error `code` → what the control shows when the **request** was refused (X-14, X-18,
 * X-19, X-21, X-22).
 *
 * **Why this map has to exist at all**, written here because the absence was the bug: three of these
 * five rejections — `too_many_export_jobs`, `rate_limited` and `service_unavailable` — create **no
 * row** (ADR-0014 §2, "no row exists on any rejection path"). There is therefore nothing for the
 * list poll to render, ever, and before this map the control simply returned from *Starting…* to its
 * idle label as though the click had not happened. The user's natural answer to silence is to click
 * again, which for the 429 and the per-session cap is exactly the behaviour those limits exist to
 * stop and which cannot ever succeed.
 *
 * `queue_unavailable` is the one that would self-heal — its job row *is* committed `failed` /
 * `not_queued`, so the poll would eventually say so — but it is named here anyway, because "you will
 * find out on the next tick" is not an error state.
 *
 * **`retryable` splits on whether asking again could plausibly work**, which is the same question
 * the server answers for job failures, kept in the same words on purpose. The two `false` rows are
 * refusals about *state*: a run that is not `succeeded` will not become exportable because the user
 * clicked twice, and a session at its 40-job cap is at its cap. The three `true` rows are about
 * *time* — a limit window, a database, a broker — and all three recover on their own.
 *
 * Keyed on `code`, never on `message`, for `DOWNLOAD_FAILURE_BY_CODE`'s reason: the server's prose
 * is prose and may be reworded without notice. X-19 is the live example — the API says "try again in
 * N seconds" because `Retry-After` is defined in seconds, while the sentence below says minutes,
 * because that is what a person needs to hear. The `code` is what binds the two.
 */
const REQUEST_FAILURE_BY_CODE: Readonly<Partial<Record<string, RequestFailureView>>> = {
  tailoring_run_not_exportable: {
    message: 'This run has no documents to download yet.',
    retryable: false,
  },
  too_many_export_jobs: {
    message: "You've reached the download limit for this session.",
    retryable: false,
  },
  rate_limited: {
    message: 'Too many exports — try again in a few minutes.',
    retryable: true,
  },
  service_unavailable: {
    message: 'Something went wrong. Try again.',
    retryable: true,
  },
  queue_unavailable: {
    message: "We couldn't start preparing your file. Try again.",
    retryable: true,
  },
};

/**
 * Anything unrecognised — including a network drop, which is not an `ApiError` at all.
 *
 * `retryable: true`, and the asymmetry with `UNKNOWN_DOWNLOAD_FAILURE` is not one: both say "we do
 * not know what happened, so let the user try". Offering a retry that fails again costs one request;
 * withholding one from a user whose export *would* have worked strands them on a page whose only
 * button does nothing.
 */
const UNKNOWN_REQUEST_FAILURE: RequestFailureView = {
  message: 'Something went wrong. Try again.',
  retryable: true,
};

/**
 * Turn a rejected `POST /exports` into the sentence its control shows and whether to offer a retry.
 *
 * **No `null` branch, unlike `downloadFailureFor`.** That function drops the 409 `export_not_ready`
 * race because the job's own polled state is a better answer than anything the client could say.
 * There is no equivalent here: a refused request left nothing to poll, so every rejection must
 * produce a sentence or the user gets nothing at all — which was the finding.
 *
 * 401 is deliberately absent from the map: a dead session is the run page's own concern and it
 * unmounts the workspace, so a per-control sentence about it would be a second, smaller answer to a
 * question already answered one level up. It falls through to the generic line in the window before
 * that happens.
 */
export function requestFailureFor(error: Error): RequestFailureView {
  if (!(error instanceof ApiError) || error.code === null) {
    return UNKNOWN_REQUEST_FAILURE;
  }
  return REQUEST_FAILURE_BY_CODE[error.code] ?? UNKNOWN_REQUEST_FAILURE;
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
