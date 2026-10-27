/**
 * The `/` gate's and the account workspace's sentences (slice 2.3, AC-38, AC-49), in one
 * module so a reworded line changes in one place.
 */

/** AC-38 / H-56: the boot refresh has not answered yet. One status line, nothing typeable. */
export const GATE_BOOTING_NOTE = 'Checking your account…';
/** AC-38 / OQ-10: the boot could not answer — neither workspace, and a Retry. */
export const GATE_UNAVAILABLE_NOTE = "Couldn't check whether you're signed in";
export const GATE_RETRY_LABEL = 'Retry';

/**
 * Slice 4.1, AC-37 (OQ-14, verbatim; Constitution §8): the operator can read stored data, and the
 * user is told so. One constant, on three surfaces: `ACCOUNT_PROMISE` below, the guest workspace
 * (beside the Gemini sentence, in `TailorLaunch`) and the saved-CV notice on `/account`.
 */
export const OPERATOR_ACCESS_NOTE =
  'The person who runs TailorCraft can read what is stored here — CVs, job postings and tailored documents — to operate and support the service.';

/** AC-49: what the account workspace promises, before the first launch. Verbatim; pinned. */
export const ACCOUNT_PROMISE = `You're signed in, so what you tailor here is saved to your history — the job posting, the tailored CV and cover letter, and any files you export — until you delete it. The AI provider sees your CV's text and the job posting when you tailor. ${OPERATOR_ACCESS_NOTE}`;

/** The latest-run query failed (a 5xx or no answer); the rest of the workspace still works. */
export const LATEST_RUN_ERROR_NOTE = "Couldn't load your latest run.";
export const RETRY_LABEL = 'Retry';

/**
 * web-design-refresh C-4: the guest step rail's captions. Decoration beside the tab names — the
 * tabs' accessible names stay *Base CV* / *Job posting*, so these are `aria-hidden` there.
 */
export const STEP_RAIL_BASE_CV_CAPTION = 'PDF, DOCX or TXT';
export const STEP_RAIL_JOB_POSTING_CAPTION = 'Text or a link';
/** OQ-21: the rail's third item — a list item, not a tab (tailoring is a section, not a panel). */
export const STEP_RAIL_TAILOR_LABEL = 'Tailor';
export const STEP_RAIL_TAILOR_CAPTION = 'Under two minutes';
/** C-4, below `sm`: where the visitor is, derived from the open tab. */
export function stepOfThree(step: 1 | 2): string {
  return `Step ${String(step)} of 3`;
}

/** C-5: the guest base-CV tab's intro line. */
export const BASE_CV_INTRO = "Let's start with your CV";
