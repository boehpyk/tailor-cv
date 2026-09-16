import type { BaseCvCheck, JobPostingCheck } from '@/features/tailoring/launchReadiness';
import type { RunView } from '@/features/tailoring/runView';

/**
 * Where one stage of the stepper stands (AC-24). Exactly one per stage, never a pair of booleans:
 * `active` and `failed` are mutually exclusive, and a union makes rendering both at once a type
 * error rather than a Tuesday.
 */
export type StageState = 'pending' | 'active' | 'done' | 'failed';

/** The three stages the stepper draws — *Extracting CV → Fetching job → Tailoring*. */
export interface ProgressView {
  readonly extracting: StageState;
  readonly fetching: StageState;
  readonly tailoring: StageState;
}

/**
 * Everything `deriveProgress` reads, and nothing it does not.
 *
 * The names mirror 1.3's `launchReadiness.ts`: `baseCv` and `jobPosting` are the same two display
 * checks the launch control is drawn from (`checkBaseCv(latestBaseCv(items))`,
 * `checkJobPosting(latestPosting(items))`), so the stepper and the checklist cannot disagree about
 * whether a CV is usable — "is this CV usable" is `status === 'extracted'`, answered by the API and
 * read here, never re-derived. The two flags are the panel mutations' `isPending`
 * (`useUploadBaseCv`, `useCreateJobPosting`), which is the only way "in flight" can be known before
 * the list has an item to show. `run` is `viewOfWatchedRun(...)`, the same derivation the run page
 * uses, so stage 3 follows the run exactly as 1.3's panel did.
 */
export interface ProgressInput {
  readonly baseCv: BaseCvCheck;
  /** `useUploadBaseCv().isPending` — an upload is on the wire. */
  readonly isUploadingBaseCv: boolean;
  readonly jobPosting: JobPostingCheck;
  /** `useCreateJobPosting().isPending` — a paste or a fetch is on the wire. */
  readonly isSubmittingJobPosting: boolean;
  readonly run: RunView;
}

/**
 * Stage 1. The wire is checked first: while an upload is on it, whatever the list says is about
 * the *previous* CV, and the user's attention is on the one going up. `reading` is `active` too —
 * 1.1 extracts in a worker thread, so a CV can be listed before its text is — and `unreadable` is
 * `failed` because that is the status the API gave it (`extraction_failed`), not a judgement made
 * here.
 */
function extractingStage(baseCv: BaseCvCheck, isUploadingBaseCv: boolean): StageState {
  if (isUploadingBaseCv) {
    return 'active';
  }
  switch (baseCv.state) {
    case 'ready':
      return 'done';
    case 'reading':
      return 'active';
    case 'unreadable':
      return 'failed';
    case 'missing':
      return 'pending';
  }
}

/**
 * Stage 2. Independent of stage 1 on purpose: the two inputs are two tabs, and a posting pasted
 * before the CV is uploaded is a posting all the same. A stepper reading *pending → done → pending*
 * is an accurate description of that session, not a bug. A failed fetch is not a `failed` stage
 * here: the posting is never created, so the list holds nothing and the stage stays `pending` while
 * 1.2's `FetchFailureNotice` in the panel says what went wrong (E-22).
 */
function fetchingStage(jobPosting: JobPostingCheck, isSubmittingJobPosting: boolean): StageState {
  if (isSubmittingJobPosting) {
    return 'active';
  }
  return jobPosting.state === 'ready' ? 'done' : 'pending';
}

/**
 * Stage 3, from the run view alone. `loading` and `unreadable` are `pending` rather than a fifth
 * state: the stepper says where the *work* stands, and a run that cannot be read is neither working
 * nor finished — the page that owns the view says so in words beside the stepper.
 */
export function tailoringStage(run: RunView): StageState {
  switch (run.kind) {
    case 'working':
      return 'active';
    case 'succeeded':
      return 'done';
    case 'failed':
      return 'failed';
    case 'none':
    case 'loading':
    case 'unreadable':
      return 'pending';
  }
}

/**
 * The stepper's three stage states from the inputs above. Pure, table-tested (AC-24).
 *
 * Each stage is derived from its own facts and nothing else. A rule such as "stage 2 cannot be done
 * while stage 1 is pending" would be a second opinion about the inputs — the launch checklist
 * already refuses to start without both, and that refusal is the API's, read through
 * `launchInput`. The stepper's job is to describe, not to gate.
 */
export function deriveProgress(input: ProgressInput): ProgressView {
  return {
    extracting: extractingStage(input.baseCv, input.isUploadingBaseCv),
    fetching: fetchingStage(input.jobPosting, input.isSubmittingJobPosting),
    tailoring: tailoringStage(input.run),
  };
}

/**
 * The stepper as the **run page** draws it. Stages 1 and 2 are `done` by construction: the API
 * accepted this run only because both inputs existed and the CV was extracted (1.3's 404 and 409
 * rules), so those two facts are settled history — and the *latest* CV in the list may by now be a
 * different one, which is why the run page does not read the lists to redraw them. Stage 3 is live.
 */
export function progressOfRun(run: RunView): ProgressView {
  return { extracting: 'done', fetching: 'done', tailoring: tailoringStage(run) };
}
