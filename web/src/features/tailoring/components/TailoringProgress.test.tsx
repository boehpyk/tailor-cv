import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TailoringProgress } from './TailoringProgress';

/**
 * F12 — `TailoringProgress`'s own test file. Its 20-second "taking longer than usual" notice
 * (AC-29) used to be exercised only through `TailorPanel.test.tsx` (deleted at F11, `433dfc1`),
 * driving it indirectly through three list-query stubs and a polled run. `TailoringProgress` takes
 * its `status` as a prop and owns no network call, so none of that is needed here — this is the
 * component's direct equivalent.
 */

describe('TailoringProgress', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('a queued run says "Waiting for a worker…" and not the running copy', () => {
    render(<TailoringProgress status="queued" />);

    expect(screen.getByText('Waiting for a worker…')).toBeInTheDocument();
    expect(screen.queryByText(/Tailoring with Gemini/)).not.toBeInTheDocument();
  });

  it('a running run says "Tailoring with Gemini…" with a live elapsed count that ticks', async () => {
    vi.useFakeTimers();
    render(<TailoringProgress status="running" />);

    const readElapsedSeconds = (): number => {
      const text = screen.getByText(/Tailoring with Gemini/).textContent;
      const match = /(\d+)\s*s\b/.exec(text);
      if (match === null) {
        throw new Error(`no elapsed seconds count found in "${text}"`);
      }
      return Number(match[1]);
    };

    const before = readElapsedSeconds();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    const after = readElapsedSeconds();

    expect(after).toBeGreaterThan(before);
  });

  it('after 20s of running, adds "This is taking longer than usual — we are still working. Don\'t refresh."', async () => {
    vi.useFakeTimers();
    render(<TailoringProgress status="running" />);

    expect(
      screen.queryByText("This is taking longer than usual — we are still working. Don't refresh."),
    ).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(21_000);
    });

    expect(
      screen.getByText("This is taking longer than usual — we are still working. Don't refresh."),
    ).toBeInTheDocument();
  });
});
