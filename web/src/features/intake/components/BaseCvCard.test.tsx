import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { BaseCvCard } from './BaseCvCard';

import type { BaseCv } from '../types';

function makeExtractedCv(overrides: Partial<BaseCv> = {}): BaseCv {
  return {
    id: '0192f0a1-0000-7000-8000-000000000001',
    original_filename: 'jane-cv.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'extracted',
    character_count: 512,
    failure_reason: null,
    failure_message: null,
    uploaded_at: '2026-09-07T10:00:00Z',
    expires_at: '2026-09-08T10:00:00Z',
    origin: 'uploaded',
    ...overrides,
  };
}

describe('BaseCvCard', () => {
  it('renders the filename, size and character count', () => {
    render(<BaseCvCard cv={makeExtractedCv()} onReplace={vi.fn()} />);

    expect(screen.getByText('jane-cv.pdf')).toBeInTheDocument();
    expect(screen.getByText('2 KB')).toBeInTheDocument();
    expect(screen.getByText('512 characters extracted')).toBeInTheDocument();
  });

  it('renders zero characters extracted when character_count is null', () => {
    render(<BaseCvCard cv={makeExtractedCv({ character_count: null })} onReplace={vi.fn()} />);

    expect(screen.getByText('0 characters extracted')).toBeInTheDocument();
  });

  it('renders "stored until" the CV\'s expiry, per technical-plan.md\'s success-state example', () => {
    render(
      <BaseCvCard
        cv={makeExtractedCv({ expires_at: '2026-09-08T10:00:00Z' })}
        onReplace={vi.fn()}
      />,
    );

    // technical-plan.md line 402: "stored until 8 Sep 10:00" for this exact `expires_at`.
    expect(screen.getByText('stored until 8 Sep 10:00')).toBeInTheDocument();
  });

  it('calls onReplace when "Replace CV" is activated', async () => {
    const user = userEvent.setup();
    const onReplace = vi.fn();
    render(<BaseCvCard cv={makeExtractedCv()} onReplace={onReplace} />);

    await user.click(screen.getByRole('button', { name: 'Replace CV' }));

    expect(onReplace).toHaveBeenCalledTimes(1);
  });

  // AC-41: a working copy (origin: 'copied_from_saved') says so — the saved CV it came from stays
  // in the account; this copy goes with the workspace.
  it('shows the "Working copy" badge and its note when origin is copied_from_saved', () => {
    render(
      <BaseCvCard cv={makeExtractedCv({ origin: 'copied_from_saved' })} onReplace={vi.fn()} />,
    );

    expect(screen.getByText('Working copy')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Copy of your saved CV — deleted with this workspace in 24 hours; your saved CV stays in your account',
      ),
    ).toBeInTheDocument();
  });

  it('shows neither the badge nor the note for an ordinary upload (origin: uploaded)', () => {
    render(<BaseCvCard cv={makeExtractedCv({ origin: 'uploaded' })} onReplace={vi.fn()} />);

    expect(screen.queryByText('Working copy')).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        'Copy of your saved CV — deleted with this workspace in 24 hours; your saved CV stays in your account',
      ),
    ).not.toBeInTheDocument();
  });
});
