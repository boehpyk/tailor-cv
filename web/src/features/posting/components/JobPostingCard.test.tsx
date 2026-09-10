import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { JobPostingCard } from './JobPostingCard';

import type { JobPostingSummary } from '../types';

function makeSummary(overrides: Partial<JobPostingSummary> = {}): JobPostingSummary {
  return {
    id: '0192f0a1-0000-7000-8000-000000000001',
    source: 'fetched',
    source_url: 'https://jobs.example.com/postings/1234',
    title: 'Senior Python Engineer',
    character_count: 4321,
    preview: 'We are looking for a senior Python engineer…',
    created_at: '2026-09-09T10:00:00Z',
    expires_at: '2026-09-10T10:00:00Z',
    ...overrides,
  };
}

describe('JobPostingCard', () => {
  it('renders the title', () => {
    render(
      <JobPostingCard
        posting={makeSummary({ title: 'Senior Python Engineer' })}
        onReplace={vi.fn()}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Senior Python Engineer' })).toBeInTheDocument();
  });

  it('falls back to "Job posting" when title is null', () => {
    render(<JobPostingCard posting={makeSummary({ title: null })} onReplace={vi.fn()} />);

    expect(screen.getByRole('heading', { name: 'Job posting' })).toBeInTheDocument();
  });

  it("renders the character count unformatted, matching BaseCvCard's convention", () => {
    // No source_url, so the count text node is not sharing its <p> with a host link — isolating
    // exactly what this test is about.
    render(
      <JobPostingCard
        posting={makeSummary({ character_count: 4321, source_url: null })}
        onReplace={vi.fn()}
      />,
    );

    expect(screen.getByText('4321 characters')).toBeInTheDocument();
  });

  it('renders the host as an external, nofollow link', () => {
    render(
      <JobPostingCard
        posting={makeSummary({ source_url: 'https://jobs.example.com/postings/1234' })}
        onReplace={vi.fn()}
      />,
    );

    const hostLink = screen.getByRole('link', { name: 'jobs.example.com' });
    expect(hostLink).toHaveAttribute('target', '_blank');
    const rel = hostLink.getAttribute('rel') ?? '';
    expect(rel).toContain('noopener');
    expect(rel).toContain('noreferrer');
    expect(rel).toContain('nofollow');
  });

  it('renders no link at all for a pasted posting with no source_url', () => {
    render(<JobPostingCard posting={makeSummary({ source_url: null })} onReplace={vi.fn()} />);

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('does not crash on a malformed source_url, and renders without a host link', () => {
    render(
      <JobPostingCard
        posting={makeSummary({ source_url: 'not a url', character_count: 42 })}
        onReplace={vi.fn()}
      />,
    );

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    // The rest of the card still rendered — the guard swallowed the parse failure rather than
    // taking the whole component down with it.
    expect(screen.getByText('42 characters')).toBeInTheDocument();
  });

  it('renders the preview and the "Stored until …" line', () => {
    render(
      <JobPostingCard
        posting={makeSummary({
          preview: 'We are looking for a senior Python engineer…',
          expires_at: '2026-09-10T10:00:00Z',
        })}
        onReplace={vi.fn()}
      />,
    );

    expect(screen.getByText('We are looking for a senior Python engineer…')).toBeInTheDocument();
    expect(screen.getByText('Stored until 10 Sep 10:00')).toBeInTheDocument();
  });

  it('calls onReplace when "Replace posting" is activated', async () => {
    const user = userEvent.setup();
    const onReplace = vi.fn();
    render(<JobPostingCard posting={makeSummary()} onReplace={onReplace} />);

    await user.click(screen.getByRole('button', { name: 'Replace posting' }));

    expect(onReplace).toHaveBeenCalledTimes(1);
  });
});
