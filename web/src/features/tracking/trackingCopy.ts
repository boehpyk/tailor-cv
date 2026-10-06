/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the parameters are the signatures qa's T27 tests compile against; T28 uses them and deletes this line. */
import type { BoardCard, Stage } from './types';

/**
 * Every sentence the board says (technical plan §7, AC-32…AC-40) — one module, so a reworded line
 * changes in one place and a test can name the constant it asserts.
 *
 * The constants are the spec's sentences verbatim. The functions build a sentence from data (a
 * count, a date, a title, an error's `code`). SKELETON (T26): every function returns `''`; T28
 * writes them.
 */

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

/** A column's heading, its count included — *"Applied (3)"* (AC-32). SKELETON: `''`. */
export function columnHeading(_stage: Stage, _count: number): string {
  return '';
}

// --- A card ---------------------------------------------------------------------------------------

/** The CV line of a card whose saved CV has since been deleted (`base_cv === null`, T-25). */
export const CV_DELETED_NOTE = 'CV deleted';
/** The link to the card's tailored documents, `/history/{runId}/cv`. */
export const OPEN_DOCUMENTS_LABEL = 'Open tailored CV';
/** The posting's own page, an external link — only when `source_url` is `http(s)` (AC-40). */
export const OPEN_POSTING_LABEL = 'Job posting';

/**
 * The card's name: its own title → else the posting's title → else the posting's preview (AC-32).
 * Read from the card, never stored. SKELETON: `''`.
 */
export function cardDisplayTitle(_card: BoardCard): string {
  return '';
}

/** The card's CV line: the label → else the filename → else *"CV deleted"* (AC-32). SKELETON: `''`. */
export function cardCvLine(_card: BoardCard): string {
  return '';
}

/** *"since 5 Oct 2026"* — from `stage_changed_at`, in UTC (AC-32). SKELETON: `''`. */
export function sinceCopy(_stageChangedAt: string): string {
  return '';
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

/** The polite live region after a move: *"Moved {title} to {stage}."* (AC-33). SKELETON: `''`. */
export function movedAnnouncement(_title: string, _stage: Stage): string {
  return '';
}

/**
 * Why a move was refused, by `code` (AC-34): 409 conflict, 404 gone, 429 rate limited, anything
 * else (503, a network failure) *"Not moved — try again."*. Never silent. SKELETON: `''`.
 */
export function moveFailureCopy(_error: unknown): string {
  return '';
}

// --- Retitle (AC-36) -------------------------------------------------------------------------------

export const EDIT_TITLE_LABEL = 'Edit title';
/** The inline input's label. */
export const TITLE_INPUT_LABEL = 'Title';
export const SAVE_TITLE_LABEL = 'Save';
export const CANCEL_TITLE_LABEL = 'Cancel';
export const CLEAR_TITLE_LABEL = 'Clear title';
export const RETITLE_PENDING_LABEL = 'Saving…';
/** Not the rule (the server's `ApplicationTitle` is) — only the counter's denominator. */
export const TITLE_MAX_CHARACTERS = 120;

/** The visible counter, *"12 / 120"* (AC-36). SKELETON: `''`. */
export function titleCounter(_length: number): string {
  return '';
}

/**
 * Why a retitle was refused: 422 → the boundary's own message (it never echoes the title); 409, 404,
 * 429, 503 → as a move's (AC-34). SKELETON: `''`.
 */
export function retitleFailureCopy(_error: unknown): string {
  return '';
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
export const TRACK_TOO_MANY_NOTE =
  'Your board holds 500 applications — remove some you no longer need.';
export const TRACK_FAILED_NOTE = 'Not added — try again.';

/** *"On your board · To apply"*, a link to `/board` (AC-38). SKELETON: `''`. */
export function onBoardLabel(_stage: Stage): string {
  return '';
}

/**
 * Why *Add to board* was refused: 409 not trackable, 409 too many, 429, anything else (503, a
 * network failure). A 409 `application_already_tracked` is a success and never reaches here.
 * SKELETON: `''`.
 */
export function trackFailureCopy(_error: unknown): string {
  return '';
}
