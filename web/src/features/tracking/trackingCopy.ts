import { ApiError } from '@/api/client';

import type { BoardCard, Stage } from './types';

/**
 * Every sentence the board says (technical plan §7, AC-32…AC-40) — one module, so a reworded line
 * changes in one place and a test can name the constant it asserts.
 *
 * The constants are the spec's sentences verbatim. The functions build a sentence from data (a
 * count, a date, a title, an error's `code`).
 *
 * **Error copy branches on `ApiError.code`, never on `message`** — `code` is the contract, `message`
 * is prose that may be reworded. Two exceptions: a 422 on a title, whose message *is* the
 * boundary's explanation (it never echoes the title), shown under the field as the spec asks; and
 * 409 `too_many_tracked_applications`, whose message carries the cap — a setting only the server
 * knows (T-15).
 */

/** The `code` of an API refusal, or `null` for anything else (a network failure, a bug). */
function codeOf(error: unknown): string | null {
  return error instanceof ApiError ? error.code : null;
}

// --- The page -----------------------------------------------------------------------------------

export const BOARD_HEADING = 'Your board';
/** The header link, shown only when authenticated (AC-32). */
export const BOARD_NAV_LABEL = 'Board';
export const BOARD_LOADING_NOTE = 'Loading your board…';
export const BOARD_LOAD_ERROR_NOTE = "Couldn't load your board";
export const RETRY_LABEL = 'Retry';
export const BOARD_EMPTY_NOTE = 'Nothing on your board yet';
/** The empty state's link, to `/history` (AC-32). */
export const BOARD_EMPTY_ACTION = 'Add tailored applications from your history';
/**
 * The retention note (ADR-0006 amendment (f)): what keeps a card and what removes it. Account data,
 * so no "24 hours" anywhere — nothing here is on a timer.
 */
export const BOARD_RETENTION_NOTE =
  'Applications stay on your board until you remove them, delete their history entry, or delete your account.';

/** The six column labels, in `STAGES` order (AC-32). */
export const STAGE_LABELS: Readonly<Record<Stage, string>> = {
  to_apply: 'To apply',
  applied: 'Applied',
  interviewing: 'Interviewing',
  offer: 'Offer',
  rejected: 'Rejected',
  withdrawn: 'Withdrawn',
};

/** A column's heading, its count included — *"Applied (3)"* (AC-32). */
export function columnHeading(stage: Stage, count: number): string {
  return `${STAGE_LABELS[stage]} (${String(count)})`;
}

// --- A card ---------------------------------------------------------------------------------------

/** The CV line of a card whose saved CV has since been deleted (`base_cv === null`, T-25). */
export const CV_DELETED_NOTE = 'CV deleted';
/** The link to the card's tailored documents, `/history/{runId}/cv`. */
export const OPEN_DOCUMENTS_LABEL = 'Open tailored CV';
/** The posting's own page, an external link — only when `source_url` is `http(s)` (AC-40). */
export const OPEN_POSTING_LABEL = 'Job posting';
/**
 * The last fallback of a card's name: no title of its own and no posting to borrow one from — a
 * card whose run row is missing (T-36), which the server's locks prevent and the board still lists.
 */
export const UNTITLED_CARD = 'Untitled application';

/**
 * The card's name: its own title → else the posting's title → else the posting's preview (AC-32).
 * Read from the card, never stored.
 */
export function cardDisplayTitle(card: BoardCard): string {
  return card.title ?? card.posting?.title ?? card.posting?.preview ?? UNTITLED_CARD;
}

/** The card's CV line: the label → else the filename → else *"CV deleted"* (AC-32). */
export function cardCvLine(card: BoardCard): string {
  return card.base_cv === null
    ? CV_DELETED_NOTE
    : (card.base_cv.label ?? card.base_cv.original_filename);
}

/** *"since 5 Oct 2026"* — from `stage_changed_at`, in UTC (AC-32). */
export function sinceCopy(stageChangedAt: string): string {
  // Reassembled from parts, like `features/intake/format.ts`: 'en-US' supplies the token spellings
  // only ("Sep", where en-GB says "Sept"), the order is ours, and UTC keeps one card reading the
  // same date for everyone.
  const parts = new Intl.DateTimeFormat('en-US', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).formatToParts(new Date(stageChangedAt));
  const find = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((part) => part.type === type)?.value ?? '';
  return `since ${find('day')} ${find('month')} ${find('year')}`;
}

// --- Move (AC-33, AC-34) -----------------------------------------------------------------------

export const MOVE_TO_LABEL = 'Move to';
/** The Move control while its request is on the wire (T-39). */
export const MOVE_PENDING_LABEL = 'Saving…';
export const MOVE_CONFLICT_NOTE =
  'This application changed in another tab — your board is up to date.';
export const MOVE_GONE_NOTE = 'This application is no longer on your board.';
export const MOVE_RATE_LIMITED_NOTE = 'Too many changes — wait a moment and try again.';
export const MOVE_FAILED_NOTE = 'Not moved — try again.';

/** The polite live region after a move: *"Moved {title} to {stage}."* (AC-33). */
export function movedAnnouncement(title: string, stage: Stage): string {
  return `Moved ${title} to ${STAGE_LABELS[stage]}.`;
}

/**
 * Why a move was refused, by `code` (AC-34): 409 conflict, 404 gone, 429 rate limited, anything
 * else (503, a network failure) *"Not moved — try again."*. Never silent.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function moveFailureCopy(error: unknown, _deadlineMs: number | null): string {
  switch (codeOf(error)) {
    case 'tracked_application_version_conflict':
      return MOVE_CONFLICT_NOTE;
    case 'tracked_application_not_found':
      return MOVE_GONE_NOTE;
    case 'rate_limited':
      return MOVE_RATE_LIMITED_NOTE;
    default:
      return MOVE_FAILED_NOTE;
  }
}

// --- Retitle (AC-36) -------------------------------------------------------------------------------

export const EDIT_TITLE_LABEL = 'Edit title';
/** The inline input's label. */
export const TITLE_INPUT_LABEL = 'Title';
export const SAVE_TITLE_LABEL = 'Save';
export const CANCEL_TITLE_LABEL = 'Cancel';
export const CLEAR_TITLE_LABEL = 'Clear title';
export const RETITLE_PENDING_LABEL = 'Saving…';
/** A retitle refused for a reason other than the title itself, a race or the rate limit (503, network). */
export const RETITLE_FAILED_NOTE = 'Title not saved — try again.';
/** Not the rule (the server's `ApplicationTitle` is) — only the counter's denominator. */
export const TITLE_MAX_CHARACTERS = 120;

/** The visible counter, *"12 / 120"* (AC-36). */
export function titleCounter(length: number): string {
  return `${String(length)} / ${String(TITLE_MAX_CHARACTERS)}`;
}

/**
 * Why a retitle was refused: 422 → the boundary's own message (it never echoes the title); 409, 404,
 * 429, 503 → as a move's (AC-34).
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function retitleFailureCopy(error: unknown, _deadlineMs: number | null): string {
  switch (codeOf(error)) {
    case 'validation_error':
      // `codeOf` returned a code, so this is an `ApiError`; the narrowing is for the type.
      return error instanceof ApiError ? error.message : RETITLE_FAILED_NOTE;
    case 'tracked_application_version_conflict':
      return MOVE_CONFLICT_NOTE;
    case 'tracked_application_not_found':
      return MOVE_GONE_NOTE;
    case 'rate_limited':
      return MOVE_RATE_LIMITED_NOTE;
    default:
      return RETITLE_FAILED_NOTE;
  }
}

// --- Untrack (AC-37) -------------------------------------------------------------------------------

export const REMOVE_LABEL = 'Remove from board';
export const REMOVE_PENDING_LABEL = 'Removing…';
export const REMOVED_NOTE = "Removed from your board. It's still in your history.";
export const REMOVE_FAILED_NOTE = 'Not removed — try again.';

// --- Add to board (AC-38) --------------------------------------------------------------------------

export const TRACK_LABEL = 'Add to board';
export const TRACK_PENDING_LABEL = 'Adding…';
export const TRACK_NOT_TRACKABLE_NOTE =
  'Only a finished tailored application can go on your board.';
export const TRACK_FAILED_NOTE = 'Not added — try again.';
/** 404 `tailoring_run_not_found`: the history entry was deleted (or was never this account's). */
export const TRACK_GONE_NOTE = 'This tailored application no longer exists.';

/** *"On your board · To apply"*, a link to `/board` (AC-38). */
export function onBoardLabel(stage: Stage): string {
  return `On your board · ${STAGE_LABELS[stage]}`;
}

/**
 * Why *Add to board* was refused: 409 not trackable, 409 too many (the server's own sentence, which
 * carries the real cap), 429, anything else (503, a network failure). A 409
 * `application_already_tracked` is a success and never reaches here.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function trackFailureCopy(error: unknown, _deadlineMs: number | null): string {
  switch (codeOf(error)) {
    case 'tailoring_run_not_trackable':
      return TRACK_NOT_TRACKABLE_NOTE;
    case 'too_many_tracked_applications':
      // The cap is a setting (`MAX_TRACKED_APPLICATIONS_PER_USER`), so only the server knows the
      // number: its sentence is shown as written. A client copy with a number in it is wrong the
      // day the setting changes. `codeOf` returned a code, so this is an `ApiError`.
      return error instanceof ApiError ? error.message : TRACK_FAILED_NOTE;
    case 'tailoring_run_not_found':
      return TRACK_GONE_NOTE;
    case 'rate_limited':
      return MOVE_RATE_LIMITED_NOTE;
    default:
      return TRACK_FAILED_NOTE;
  }
}
