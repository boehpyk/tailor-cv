import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { TailoringFailureNotice } from './TailoringFailureNotice';

/**
 * T44 — structure and markup for `TailoringFailureNotice` (error C): role and accessible name, and
 * the hint line's conditional rendering (task-list T44). `TailorPanel.test.tsx`'s `it.each` over
 * `FAILURE_REASON_CASES` already covers, per `failure_reason`, the headline copy and whether "Try
 * again" is offered at all — not repeated here. What is new: the role itself, that the opening
 * "this run did not finish" line is part of the same alert (so a screen reader gets it in one
 * announcement), the hint's presence/absence, and the button's `disabled`/label wiring for
 * `canStart` and `isStarting`, neither of which the behavioural suite exercises.
 */

describe('TailoringFailureNotice', () => {
  it('renders as an alert — error C interrupts the working state same as error B interrupts nothing', () => {
    render(
      <TailoringFailureNotice
        reason="llm_unavailable"
        retryable
        canStart
        isStarting={false}
        onRetry={vi.fn()}
      />,
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent("We couldn't reach the model.");
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('keeps the "did not finish" framing and the headline inside one alert region', () => {
    render(
      <TailoringFailureNotice
        reason="llm_timed_out"
        retryable
        canStart
        isStarting={false}
        onRetry={vi.fn()}
      />,
    );

    const alert = screen.getByRole('alert');
    // Spec-adjacent: the failure contract distinguishes "the API refused" (error B) from "a run was
    // accepted and did not finish" (error C); T42 chose to say so explicitly rather than rely on
    // colour, and both sentences must reach an assistive-tech user as one announcement.
    expect(alert).toHaveTextContent('This tailoring run did not finish');
    expect(alert).toHaveTextContent('That took too long.');
  });

  it('renders a hint line only for reasons whose copy carries one', () => {
    const { rerender } = render(
      <TailoringFailureNotice
        reason="llm_rate_limited"
        retryable
        canStart
        isStarting={false}
        onRetry={vi.fn()}
      />,
    );
    // llm_rate_limited is the one reason in failureCopy.ts with a non-null hint.
    expect(screen.getByText('Give it a minute before you try again.')).toBeInTheDocument();

    rerender(
      <TailoringFailureNotice
        reason="llm_unavailable"
        retryable
        canStart
        isStarting={false}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.queryByText('Give it a minute before you try again.')).not.toBeInTheDocument();
  });

  it('disables Try again while a retry is starting, and labels it accordingly', () => {
    render(
      <TailoringFailureNotice
        reason="llm_unavailable"
        retryable
        canStart
        isStarting={true}
        onRetry={vi.fn()}
      />,
    );

    const button = screen.getByRole('button', { name: 'Starting…' });
    expect(button).toBeDisabled();
  });

  it('disables Try again when the inputs are no longer available to retry with', () => {
    render(
      <TailoringFailureNotice
        reason="llm_unavailable"
        retryable
        canStart={false}
        isStarting={false}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: 'Try again' })).toBeDisabled();
  });

  it('renders a fallback headline for a null reason without crashing', () => {
    render(
      <TailoringFailureNotice
        reason={null}
        retryable={false}
        canStart
        isStarting={false}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Something went wrong generating your documents.',
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
