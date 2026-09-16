import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ProgressStepper } from './ProgressStepper';

import type { ProgressView } from '../progress';
import type { RunView } from '@/features/tailoring/runView';

/**
 * F6 RED — `ProgressStepper`'s rendered states (feature-spec AC-24; technical-plan "Frontend"
 * §`ProgressStepper.tsx`: "three `<li>`s with the stage label and an `aria-current="step"` on the
 * active one; the stage-3 body embeds 1.3's `TailoringProgress` unchanged when active and
 * `TailoringFailureNotice` unchanged when failed").
 *
 * Written against the **spec**, not `ProgressStepper.tsx`'s F5 skeleton, which renders the three
 * `STAGE_LABELS` and nothing about state — no `aria-current`, no per-stage marker, no stage-3 body
 * — so every assertion below fails on a real mismatch (an absent attribute, absent text), never an
 * `ImportError`.
 *
 * **The chosen observable** (task-list F6: "pick an observable the implementer must honour and
 * state it in the test docblock"): each stage's `<li>` carries `data-state="pending" | "active" |
 * "done" | "failed"`, matching `StageState` exactly. `aria-current="step"` is asserted
 * *additionally*, only on whichever `<li>` is `active` — the ARIA attribute says "this one is
 * current" to assistive tech, `data-state` is what lets this test (and any other) tell all four
 * states apart, including `done` and `failed`, neither of which `aria-current` can express.
 */

const ALL_PENDING: ProgressView = {
  extracting: 'pending',
  fetching: 'pending',
  tailoring: 'pending',
};
const NO_RUN: RunView = { kind: 'none' };

function stageItem(label: 'Extracting CV' | 'Fetching job' | 'Tailoring'): HTMLElement {
  return screen.getByText(label).closest('li') as HTMLElement;
}

describe('ProgressStepper', () => {
  it('renders the three stage labels, in order, none current when everything is pending', () => {
    render(
      <ProgressStepper
        progress={ALL_PENDING}
        run={NO_RUN}
        canStart={false}
        isStarting={false}
        onRetry={() => undefined}
      />,
    );

    const items = screen.getAllByRole('listitem');
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining('Extracting CV'),
      expect.stringContaining('Fetching job'),
      expect.stringContaining('Tailoring'),
    ]);
    for (const item of items) {
      expect(item).toHaveAttribute('data-state', 'pending');
      expect(item).not.toHaveAttribute('aria-current');
    }
  });

  it('marks a done stage as done, not current', () => {
    render(
      <ProgressStepper
        progress={{ extracting: 'done', fetching: 'pending', tailoring: 'pending' }}
        run={NO_RUN}
        canStart={false}
        isStarting={false}
        onRetry={() => undefined}
      />,
    );

    expect(stageItem('Extracting CV')).toHaveAttribute('data-state', 'done');
    expect(stageItem('Extracting CV')).not.toHaveAttribute('aria-current');
  });

  it('marks a failed stage as failed, not current', () => {
    render(
      <ProgressStepper
        progress={{ extracting: 'failed', fetching: 'pending', tailoring: 'pending' }}
        run={NO_RUN}
        canStart={false}
        isStarting={false}
        onRetry={() => undefined}
      />,
    );

    expect(stageItem('Extracting CV')).toHaveAttribute('data-state', 'failed');
    expect(stageItem('Extracting CV')).not.toHaveAttribute('aria-current');
  });

  it('marks the active stage current, and renders 1.3\'s "Waiting for a worker…" for a queued run', () => {
    render(
      <ProgressStepper
        progress={{ extracting: 'done', fetching: 'done', tailoring: 'active' }}
        run={{ kind: 'working', runId: 'run-1', status: 'queued' }}
        canStart={false}
        isStarting={false}
        onRetry={() => undefined}
      />,
    );

    expect(stageItem('Tailoring')).toHaveAttribute('data-state', 'active');
    expect(stageItem('Tailoring')).toHaveAttribute('aria-current', 'step');
    // Byte-identical to 1.3's `TailoringProgress` (`components/TailoringProgress.tsx`).
    expect(screen.getByText('Waiting for a worker…')).toBeInTheDocument();
  });

  it('renders 1.3\'s "Tailoring with Gemini…" for a running run', () => {
    render(
      <ProgressStepper
        progress={{ extracting: 'done', fetching: 'done', tailoring: 'active' }}
        run={{ kind: 'working', runId: 'run-1', status: 'running' }}
        canStart={false}
        isStarting={false}
        onRetry={() => undefined}
      />,
    );

    expect(stageItem('Tailoring')).toHaveAttribute('aria-current', 'step');
    expect(screen.getByText(/Tailoring with Gemini/)).toBeInTheDocument();
  });

  it("a failed run renders 1.3's TailoringFailureNotice, with Try again only when retryable", () => {
    render(
      <ProgressStepper
        progress={{ extracting: 'done', fetching: 'done', tailoring: 'failed' }}
        run={{ kind: 'failed', reason: 'llm_timed_out', retryable: true }}
        canStart={true}
        isStarting={false}
        onRetry={() => undefined}
      />,
    );

    expect(stageItem('Tailoring')).toHaveAttribute('data-state', 'failed');
    expect(stageItem('Tailoring')).not.toHaveAttribute('aria-current');
    // failureCopy.ts's headline for `llm_timed_out`.
    expect(screen.getByText('That took too long.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('offers no Try again for a non-retryable failure', () => {
    render(
      <ProgressStepper
        progress={{ extracting: 'done', fetching: 'done', tailoring: 'failed' }}
        run={{ kind: 'failed', reason: 'llm_refused', retryable: false }}
        canStart={true}
        isStarting={false}
        onRetry={() => undefined}
      />,
    );

    // failureCopy.ts's headline for `llm_refused`.
    expect(screen.getByText('The model declined to rewrite this content.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });
});
