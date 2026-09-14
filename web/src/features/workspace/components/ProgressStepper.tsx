import type { ProgressView } from '../progress';
import type { RunView } from '@/features/tailoring/runView';

export interface ProgressStepperProps {
  /** `deriveProgress(...)` — which of the three stages is pending, active, done or failed. */
  readonly progress: ProgressView;
  /**
   * The watched run, for stage 3's body: 1.3's `TailoringProgress` (keyed on run id + status)
   * while working, 1.3's `TailoringFailureNotice` when failed. Only the `working` and `failed`
   * members are read; the stage *state* comes from `progress`, never re-derived from here.
   */
  readonly run: RunView;
  /** `TailoringFailureNotice`'s three props, passed through unchanged. */
  readonly canStart: boolean;
  readonly isStarting: boolean;
  readonly onRetry: () => void;
}

/** AC-24's labels, in order. The words are the contract; the states are not decided here. */
const STAGE_LABELS = ['Extracting CV', 'Fetching job', 'Tailoring'] as const;

/**
 * The three-stage progress stepper — presentational (AC-24).
 *
 * F5 skeleton: an ordered list of the three labels and nothing about their state. `aria-current`,
 * the per-stage bodies (1.3's progress and failure components, unchanged) arrive in F7 against qa's
 * red tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- skeleton: read in F7
export function ProgressStepper(props: ProgressStepperProps): React.JSX.Element {
  return (
    <ol aria-label="Tailoring progress">
      {STAGE_LABELS.map((label) => (
        <li key={label}>{label}</li>
      ))}
    </ol>
  );
}
