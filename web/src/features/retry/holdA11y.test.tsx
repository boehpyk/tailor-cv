import { act, render, screen } from '@testing-library/react';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { TailorLaunch } from '../tailoring/components/TailorLaunch';
import { TailoringFailureNotice } from '../tailoring/components/TailoringFailureNotice';

import { ConnectionNote } from './components/ConnectionNote';
import { HoldNote } from './components/HoldNote';

/**
 * T23 (after; slice 3.3, AC-23) — markup and accessibility of the hold and connection surfaces.
 * Test-after on purpose: the markup was shaped against the components, then pinned here.
 *
 * What is pinned: the hold sentence is fixed text, once, in the owner's existing alert or disabled
 * reason (never a ticking number inside a live region); the per-second countdown exists only at
 * ≤ 90 s and is `aria-hidden`; the end of a hold is a polite `role="status"`; nothing on these
 * surfaces animates, so `prefers-reduced-motion` has nothing to reduce; and a held button keeps its
 * accessible name, with the reason reachable through `aria-describedby`.
 */

const NOW = 1_700_000_000_000;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

function occurrences(text: string, needle: RegExp): number {
  return (text.match(new RegExp(needle.source, 'g')) ?? []).length;
}

describe('HoldNote', () => {
  it('shows an aria-hidden per-second count at 90 s and below', () => {
    render(<HoldNote held remainingSeconds={90} released={false} />);

    const count = screen.getByText('90 s');
    expect(count).toHaveAttribute('aria-hidden', 'true');
    expect(count.closest('[role="status"], [role="alert"]')).toBeNull();
  });

  it("shows no count above 90 s: the sentence's clock time is enough", () => {
    render(<HoldNote held remainingSeconds={91} released={false} />);

    expect(screen.queryByText(/\d+ s$/)).not.toBeInTheDocument();
  });

  it('says "You can try again now." as a polite role="status" once released', () => {
    render(<HoldNote held={false} remainingSeconds={0} released />);

    expect(screen.getByRole('status')).toHaveTextContent('You can try again now.');
  });

  it('says nothing when never held (a run that failed hours ago released nothing)', () => {
    render(<HoldNote held={false} remainingSeconds={0} released={false} />);

    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });

  it('announces the release in a status region that already existed while held', () => {
    // Many screen readers announce only a change *inside* a live region that was already in the
    // DOM; a region mounted together with its text may never be read (verify's MINOR 5).
    const { rerender } = render(<HoldNote held remainingSeconds={30} released={false} />);
    const region = screen.getByRole('status');
    expect(region).toBeEmptyDOMElement();

    rerender(<HoldNote held={false} remainingSeconds={0} released />);

    expect(screen.getByRole('status')).toBe(region);
    expect(region).toHaveTextContent('You can try again now.');
  });

  it('can join a status region it sits in, instead of nesting a second one', () => {
    render(
      <div role="status">
        <HoldNote held={false} remainingSeconds={0} released announce={false} />
      </div>,
    );

    expect(screen.getAllByRole('status')).toHaveLength(1);
    expect(screen.getByRole('status')).toHaveTextContent('You can try again now.');
  });
});

describe('TailoringFailureNotice while held', () => {
  const props = {
    reason: 'llm_rate_limited',
    retryable: true,
    canStart: true,
    isStarting: false,
    onRetry: () => undefined,
  } as const;

  it('keeps the button name, describes it by the one fixed sentence, and releases as a status', async () => {
    const { rerender, container } = render(<TailoringFailureNotice {...props} holdUntil={null} />);
    const nameBefore = screen.getByRole('button').textContent;
    expect(screen.getByRole('button')).toBeEnabled(); // control

    rerender(<TailoringFailureNotice {...props} holdUntil={NOW + 45_000} />);

    const button = screen.getByRole('button', { name: 'Try again' });
    expect(button.textContent).toBe(nameBefore);
    expect(button).toBeDisabled();
    const reasonId = button.getAttribute('aria-describedby');
    expect(reasonId).not.toBeNull();
    expect(document.getElementById(reasonId ?? '')).toHaveTextContent(
      'You can try again in 45 seconds.',
    );
    const alert = screen.getByRole('alert');
    expect(occurrences(alert.textContent, /try again in \d+ seconds/)).toBe(1);
    expect(screen.getByText('45 s')).toHaveAttribute('aria-hidden', 'true');
    expect(container.innerHTML).not.toMatch(/animate-|transition/);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(45_000);
    });

    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled();
    expect(screen.getByRole('status')).toHaveTextContent('You can try again now.');
    expect(screen.queryByText(/\d+ s$/)).not.toBeInTheDocument();
  });
});

describe('TailorLaunch while held', () => {
  const props = {
    baseCv: { state: 'ready', baseCv: { original_filename: 'cv.pdf' } },
    jobPosting: { state: 'ready', jobPosting: { title: 'Engineer' } },
    hasPreviousRun: true,
    isStarting: false,
    onLaunch: () => undefined,
  } as unknown as Parameters<typeof TailorLaunch>[0];

  it('keeps its label, is described by the reason, and the sentence appears once', () => {
    const { container, rerender } = render(<TailorLaunch {...props} holdUntil={null} />);
    const nameBefore = screen.getByRole('button').textContent;
    expect(screen.getByRole('button')).toBeEnabled(); // control

    rerender(<TailorLaunch {...props} holdUntil={NOW + 120_000} />);

    const button = screen.getByRole('button');
    expect(button.textContent).toBe(nameBefore);
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(/You can try again at /);
    expect(occurrences(container.textContent, /You can try again at /)).toBe(1);
    expect(screen.queryByText(/\d+ s$/)).not.toBeInTheDocument(); // 120 s: no count
    expect(container.innerHTML).not.toMatch(/animate-|transition/);
  });
});

describe('ConnectionNote', () => {
  it('is one role="status" line, fixed text, for a retrying poll', () => {
    const { container } = render(
      <ConnectionNote kind="run" connection="retrying" failureCount={1} maxRetries={3} />,
    );

    const status = screen.getByRole('status');
    expect(status).toHaveTextContent(
      'Connection trouble — trying again (attempt 2 of 4). Your run is still working.',
    );
    expect(container.innerHTML).not.toMatch(/animate-|transition/);
  });

  it('is one role="status" line for an offline poll, and nothing when ok', () => {
    const { container, rerender } = render(
      <ConnectionNote kind="export" connection="paused" failureCount={0} maxRetries={3} />,
    );
    expect(screen.getByRole('status')).toHaveTextContent("You're offline.");

    rerender(<ConnectionNote kind="export" connection="ok" failureCount={0} maxRetries={3} />);
    expect(container.textContent).toBe('');
  });

  it('speaks inside a status region that already existed while the poll was ok', () => {
    const { rerender } = render(
      <ConnectionNote kind="run" connection="ok" failureCount={0} maxRetries={3} />,
    );
    const region = screen.getByRole('status');
    expect(region).toBeEmptyDOMElement();

    rerender(<ConnectionNote kind="run" connection="retrying" failureCount={1} maxRetries={3} />);

    expect(screen.getByRole('status')).toBe(region);
    expect(region).toHaveTextContent('Connection trouble');
  });
});

describe('nothing on the hold and connection markup animates', () => {
  const dir = join(dirname(fileURLToPath(import.meta.url)), 'components');

  it('no animate-* or transition class in features/retry/components', () => {
    const files = readdirSync(dir).filter(
      (name) => name.endsWith('.tsx') && !name.includes('.test.'),
    );
    expect(files.length).toBeGreaterThanOrEqual(2); // control: the scan sees the components
    for (const name of files) {
      expect(readFileSync(join(dir, name), 'utf8'), name).not.toMatch(/animate-|transition/);
    }
  });
});
