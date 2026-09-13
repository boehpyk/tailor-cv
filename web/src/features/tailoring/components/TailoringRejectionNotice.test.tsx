import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { TailoringRejectionNotice } from './TailoringRejectionNotice';

/**
 * T44 — structure and markup for `TailoringRejectionNotice` (error B): role and accessible name
 * (task-list T44). `TailorPanel.test.tsx`'s error-B tests already cover the code-mapped copy
 * (never the server's `message`) and are not repeated here.
 */

describe('TailoringRejectionNotice', () => {
  it('renders as an alert, not a status region — error B is an urgent, unrequested interruption', () => {
    render(
      <TailoringRejectionNotice
        message="You already have a tailoring run in progress."
        onViewActiveRun={null}
      />,
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('You already have a tailoring run in progress.');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('omits "View the run in progress" when no active run is named', () => {
    render(
      <TailoringRejectionNotice message="That request wasn't valid." onViewActiveRun={null} />,
    );

    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('offers "View the run in progress" only when an active run is named, and it is inside the alert', () => {
    const onViewActiveRun = vi.fn();
    render(
      <TailoringRejectionNotice
        message="You already have a tailoring run in progress."
        onViewActiveRun={onViewActiveRun}
      />,
    );

    const alert = screen.getByRole('alert');
    const button = screen.getByRole('button', { name: 'View the run in progress' });
    expect(alert).toContainElement(button);
  });

  it('calls onViewActiveRun exactly once when the control is activated', async () => {
    const user = userEvent.setup();
    const onViewActiveRun = vi.fn();
    render(
      <TailoringRejectionNotice
        message="You already have a tailoring run in progress."
        onViewActiveRun={onViewActiveRun}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'View the run in progress' }));

    expect(onViewActiveRun).toHaveBeenCalledTimes(1);
  });
});
