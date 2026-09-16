import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';

import { makeRunSummary } from '@/test/fixtures';

import { LatestRunCard } from './LatestRunCard';

import type { ReactElement } from 'react';

/**
 * `LatestRunCard` renders a react-router `<Link>` for its active/succeeded states (an `<a href>`
 * would be a full reload that empties the query cache — see `NotFoundPage.tsx`'s reasoning), and
 * `Link` throws when mounted outside a router context. A bare `MemoryRouter` is enough here — this
 * is a presentational-component test, not a navigation test, so the real route table
 * (`renderWithRouter`) would be more than this file needs.
 */
function renderWithMemoryRouter(ui: ReactElement): ReturnType<typeof render> {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

/**
 * F6 RED — `LatestRunCard`'s three sentences (feature-spec AC-25; technical-plan "Frontend"
 * §`LatestRunCard.tsx`: "the three sentences of AC-25").
 *
 * Written against the **spec**, not `LatestRunCard.tsx`'s F5 skeleton, which renders an empty
 * `<section />` regardless of `run` — so every case below fails on a genuine "text not found",
 * never an `ImportError`.
 *
 * `LatestRunCardProps` is `{ run: TailoringRunSummary | null }` only — no `canStart`/`onRetry` —
 * so a failed run's rendering is asserted only on its **copy** (`failureCopy.ts`'s headline, the
 * same sentence 1.3 uses), not on an interactive retry control: the launch's own **Try again**
 * lives on `TailorLaunch`/`ProgressStepper`, which do carry those props (see their own test files).
 */

describe('LatestRunCard', () => {
  it('shows nothing run-specific when there is no run yet', () => {
    render(<LatestRunCard run={null} />);

    expect(screen.queryByText(/in progress/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/open your tailored documents/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it.each(['queued', 'running'] as const)(
    'an active (%s) run shows "In progress" and a View link to the run',
    (status) => {
      const run = makeRunSummary({ id: 'run-active-1', status });
      renderWithMemoryRouter(<LatestRunCard run={run} />);

      expect(screen.getByText(/in progress/i)).toBeInTheDocument();
      const link = screen.getByRole('link', { name: /view/i });
      expect(link.getAttribute('href')).toMatch(/^\/runs\/run-active-1(\/|$)/);
    },
  );

  it('a succeeded run offers "Open your tailored documents", linking to the run', () => {
    const run = makeRunSummary({
      id: 'run-succeeded-1',
      status: 'succeeded',
      tailored_cv_character_count: 500,
      cover_letter_character_count: 200,
    });
    renderWithMemoryRouter(<LatestRunCard run={run} />);

    const link = screen.getByRole('link', { name: /open your tailored documents/i });
    expect(link.getAttribute('href')).toMatch(/^\/runs\/run-succeeded-1(\/|$)/);
  });

  it("a failed run shows 1.3's failure copy for its reason", () => {
    const run = makeRunSummary({
      id: 'run-failed-1',
      status: 'failed',
      failure_reason: 'llm_timed_out',
      retryable: true,
    });
    render(<LatestRunCard run={run} />);

    // failureCopy.ts's headline for `llm_timed_out` — byte-identical to 1.3's copy.
    expect(screen.getByText('That took too long.')).toBeInTheDocument();
  });
});
