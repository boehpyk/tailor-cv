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
 * The stepper's three stage states from the inputs above. Pure, table-tested (AC-24).
 *
 * F5 skeleton: the signature is the contract; the body arrives in F7 against qa's red table.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- skeleton: read in F7
export function deriveProgress(input: ProgressInput): ProgressView {
  throw new Error('not implemented');
}
