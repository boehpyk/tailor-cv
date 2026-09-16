import { TailoringFailureNotice } from '@/features/tailoring/components/TailoringFailureNotice';
import { TailoringProgress } from '@/features/tailoring/components/TailoringProgress';

import type { ProgressView, StageState } from '../progress';
import type { RunView } from '@/features/tailoring/runView';
import type { ReactNode } from 'react';

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
 * The marker glyph and its colour per state. Decoration only: `data-state` is the machine-readable
 * fact, the label's styling is the sighted user's cue, and `aria-current="step"` is the screen
 * reader's — so the glyph is `aria-hidden` and nothing depends on it.
 */
const STAGE_MARKER: Readonly<Record<StageState, { glyph: string; className: string }>> = {
  pending: { glyph: '○', className: 'text-slate-400' },
  active: { glyph: '◐', className: 'text-slate-900' },
  done: { glyph: '✓', className: 'text-emerald-600' },
  failed: { glyph: '✕', className: 'text-amber-700' },
};

const STAGE_LABEL_CLASS: Readonly<Record<StageState, string>> = {
  pending: 'text-slate-500',
  active: 'font-medium text-slate-900',
  done: 'text-slate-800',
  failed: 'font-medium text-amber-900',
};

function Stage({
  label,
  state,
  body = null,
}: {
  readonly label: string;
  readonly state: StageState;
  /** What the stage has to say beneath its label, if anything. */
  readonly body?: ReactNode;
}): React.JSX.Element {
  const marker = STAGE_MARKER[state];
  return (
    <li data-state={state} aria-current={state === 'active' ? 'step' : undefined}>
      <div className="flex items-baseline gap-2 text-sm">
        <span aria-hidden="true" className={marker.className}>
          {marker.glyph}
        </span>
        <span className={STAGE_LABEL_CLASS[state]}>{label}</span>
      </div>
      {body !== null && <div className="mt-2 ml-6">{body}</div>}
    </li>
  );
}

/**
 * The three-stage progress stepper — presentational (AC-24).
 *
 * Each `<li>` carries `data-state` (all four states, for anything that needs to tell `done` from
 * `failed`) and `aria-current="step"` on the active one only — the ARIA attribute can say "this
 * one is current" and nothing else, which is why both exist.
 *
 * **Stage 3 embeds 1.3's components unchanged.** `TailoringProgress` is keyed on run id + status,
 * exactly as 1.3's panel keyed it, so `queued → running` mounts a fresh clock and "3s" means three
 * seconds *of Gemini*; `TailoringFailureNotice` gets the run's `retryable` as the API sent it and
 * decides **Try again** from that alone. Stages 1 and 2 have no body here: their working and
 * failed states are told by the panels that own them (1.1's and 1.2's notices, E-21/E-22), and the
 * stepper only marks them.
 *
 * The body is rendered only when the run view agrees with the stage state (`active` with a
 * `working` run, `failed` with a `failed` run). The two are derived from the same view, so they
 * cannot disagree in practice — but the narrowing is what gives the JSX a `status` and a `reason`
 * to render without an assertion.
 */
export function ProgressStepper({
  progress,
  run,
  canStart,
  isStarting,
  onRetry,
}: ProgressStepperProps): React.JSX.Element {
  const [extractingLabel, fetchingLabel, tailoringLabel] = STAGE_LABELS;

  let tailoringBody: ReactNode = null;
  if (progress.tailoring === 'active' && run.kind === 'working') {
    tailoringBody = <TailoringProgress key={`${run.runId}:${run.status}`} status={run.status} />;
  } else if (progress.tailoring === 'failed' && run.kind === 'failed') {
    tailoringBody = (
      <TailoringFailureNotice
        reason={run.reason}
        retryable={run.retryable}
        canStart={canStart}
        isStarting={isStarting}
        onRetry={onRetry}
      />
    );
  }

  return (
    <ol aria-label="Tailoring progress" className="space-y-3">
      <Stage label={extractingLabel} state={progress.extracting} />
      <Stage label={fetchingLabel} state={progress.fetching} />
      <Stage label={tailoringLabel} state={progress.tailoring} body={tailoringBody} />
    </ol>
  );
}
