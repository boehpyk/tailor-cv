import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { FetchFailureNotice } from './FetchFailureNotice';

describe('FetchFailureNotice', () => {
  it('renders the message inside an alert', () => {
    render(
      <FetchFailureNotice
        message="We couldn't read that job posting from the link you gave us."
        url="https://jobs.example.com/postings/blocked"
        onPasteInstead={vi.fn()}
      />,
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent("We couldn't read that job posting from the link you gave us.");
  });

  it('renders the URL as a link with the same three rel tokens and a matching href', () => {
    const url = 'https://jobs.example.com/postings/blocked';
    render(<FetchFailureNotice message="Failed." url={url} onPasteInstead={vi.fn()} />);

    const link = screen.getByRole('link', { name: url });
    expect(link).toHaveAttribute('href', url);
    const rel = link.getAttribute('rel') ?? '';
    expect(rel).toContain('noopener');
    expect(rel).toContain('noreferrer');
    expect(rel).toContain('nofollow');
  });

  it('renders no URL block when url is empty', () => {
    render(<FetchFailureNotice message="Failed." url="" onPasteInstead={vi.fn()} />);

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('calls onPasteInstead when "Paste the description instead" is activated', async () => {
    const user = userEvent.setup();
    const onPasteInstead = vi.fn();
    render(
      <FetchFailureNotice
        message="Failed."
        url="https://jobs.example.com/postings/blocked"
        onPasteInstead={onPasteInstead}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Paste the description instead' }));

    expect(onPasteInstead).toHaveBeenCalledTimes(1);
  });
});
