import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { GuestPurgeStatus } from './GuestPurgeStatus';

import type { GuestPurgeStatus as GuestPurgeJob } from '@/api/health';

/**
 * T36 RED — the Retention block's states, queried by what a person reads (AC-35, AC-36).
 *
 * **Which component owns which of AC-35's four states, because only two live here.** *Loading* and
 * *error* belong to `SystemStatus`: it holds the query, so it is the only thing that knows whether
 * a request is in flight or failed, and its existing `isPending` / `isError` branches already cover
 * this block — "the API could not be reached" stays a different fact from "the API says the purge
 * is stale", and collapsing them is the small dishonesty that makes a status page useless. Those
 * two are asserted in `SystemStatus.test.tsx` and are deliberately **not** duplicated here.
 * *Empty* and *success* are this component's, and `success` has three readings.
 *
 * The stub renders `stub:unreported` / `stub:unscheduled` / `stub:stale` / `stub:healthy` — strings
 * chosen at T35 so that they match no plausible copy assertion. Every test below therefore fails on
 * **text**, against a component that already renders four reachable, distinguishable branches, so a
 * red here means the assertion discriminates rather than that a file was missing.
 */

function job(overrides: Partial<GuestPurgeJob> = {}): GuestPurgeJob {
  return {
    scheduled: true,
    last_run: '2026-09-20T09:48:00Z',
    last_run_age_seconds: 720,
    last_outcome: 'ok',
    overdue: 0,
    stale: false,
    detail: null,
    ...overrides,
  };
}

describe('GuestPurgeStatus — empty', () => {
  it('renders "not reported" when the API carried no jobs member (R-32)', () => {
    render(<GuestPurgeStatus job={undefined} />);

    // An older API during a deploy. Not an error — nothing failed; this deployment simply does not
    // report the job. A confident "0 expired sessions waiting" here would be the client inventing
    // a fact it was never told.
    expect(screen.getByText(/not reported/i)).toBeInTheDocument();
    expect(screen.queryByText(/expired sessions waiting/i)).not.toBeInTheDocument();
  });
});

describe('GuestPurgeStatus — success, three readings', () => {
  it('healthy: says when it last ran and that nothing is waiting', () => {
    render(<GuestPurgeStatus job={job()} />);

    expect(screen.getByText(/guest purge ran 12 minutes ago/i)).toBeInTheDocument();
    expect(screen.getByText(/0 expired sessions waiting/i)).toBeInTheDocument();
  });

  it('not scheduled: says the schedule is off, in plain words', () => {
    const { container } = render(<GuestPurgeStatus job={job({ scheduled: false, overdue: 10 })} />);

    // The sentence the whole flag exists to make visible. A config file nobody re-reads is not a
    // signal; this is (AC-35, ADR-0018 decision 5).
    //
    // Asserted against the paragraph's `textContent` rather than with `getByText`, because the
    // copy emphasises the operative word — `is <strong>off</strong>` — and `getByText` matches
    // within a single element, so it would fail on a sentence a person reads as one sentence.
    // The emphasis is the spec's and it is worth keeping: "off" is the word an operator has to
    // see. **The matcher was wrong here, not the component** — a test that dropped the `<strong>`
    // to make itself pass would have been the test bending the UI to suit its query.
    const sentence = container.textContent;
    expect(sentence).toMatch(/scheduled guest purge is off/i);
    expect(sentence).toMatch(/deleted only when someone runs it by hand/i);
    expect(sentence).toMatch(/10 expired sessions waiting/i);
    expect(sentence).not.toMatch(/has not run for over/i);
  });

  it('stale: says it has not run for over three hours', () => {
    render(
      <GuestPurgeStatus job={job({ stale: true, last_run_age_seconds: 14_400, overdue: 37 })} />,
    );

    expect(screen.getByText(/has not run for over 3 hours/i)).toBeInTheDocument();
    expect(screen.getByText(/37 expired sessions waiting/i)).toBeInTheDocument();
  });

  // GREEN ON ARRIVAL, and recorded as such rather than counted as a red. The stub renders
  // `stub:stale`, which contains neither competing sentence, so this passes against a component
  // that has no copy at all. It is kept because it guards a real regression *after* T37 — a
  // component that fell back to rendering two branches — and it is mutation-verified there.
  it('the three readings are mutually exclusive — never two sentences in one frame', () => {
    render(<GuestPurgeStatus job={job({ stale: true, overdue: 1 })} />);

    expect(screen.queryByText(/scheduled guest purge is off/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/guest purge ran/i)).not.toBeInTheDocument();
  });

  it('a backlog that could not be counted reads as unknown, never as zero (R-29)', () => {
    render(<GuestPurgeStatus job={job({ overdue: null })} />);

    // Dropping the clause is the dangerous choice: an operator reading a sentence with no backlog
    // in it infers there is none. `overdue` is the number the runbook says to trust over the
    // heartbeat, so "could not be counted" must not look like "nothing is waiting".
    expect(screen.queryByText(/0 expired sessions waiting/i)).not.toBeInTheDocument();
    expect(screen.getByText(/unknown/i)).toBeInTheDocument();
  });
});

describe('GuestPurgeStatus — AC-36, nothing identifying is rendered', () => {
  /**
   * **Rendered output, not source text, and that distinction is the whole test.** Grepping this
   * module's source for the word "path" would pass while the component happily interpolated a
   * storage key, because the key never appears in the source — it arrives in a prop. So the
   * component is rendered with a job whose every nullable field is populated, including `detail`,
   * and the resulting DOM is searched for the shapes that identify a person or a machine.
   *
   * `detail` is seeded with a value that is simultaneously a UUID, a filename and a path, so a
   * component that rendered it fails on all three needles at once rather than on whichever one the
   * server happened to send that day.
   */
  // GREEN ON ARRIVAL, for the same reason and with the same obligation: the stub renders
  // `stub:healthy` and could not leak a key if it tried. A privacy assertion that passes because
  // the component renders nothing proves nothing — so T37 mutation-verifies it by rendering
  // `detail` and watching this test redden. Until then it is scaffolding, not evidence.
  it('renders no UUID, no filename, no storage key and no path', () => {
    const { container } = render(
      <GuestPurgeStatus
        job={job({
          detail: '/var/lib/tailorcraft/uploads/01/a0/01a0a4da-f4e4-7fc0-b961-5eee41229f13.pdf',
        })}
      />,
    );

    const rendered = container.textContent;

    expect(rendered).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    expect(rendered).not.toMatch(/\.(pdf|docx|txt)\b/i);
    expect(rendered).not.toMatch(/\//);
    expect(rendered).not.toMatch(/uploads/i);
  });
});
