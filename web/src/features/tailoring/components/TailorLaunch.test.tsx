import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { TailorLaunch } from './TailorLaunch';

import type { BaseCvCheck, JobPostingCheck } from '../launchReadiness';
import type { BaseCv } from '@/features/intake/types';
import type { JobPostingSummary } from '@/features/posting/types';

/**
 * T44 — structure and markup for `TailorLaunch`: the prerequisites checklist and the disclosure's
 * placement relative to the button (task-list T44). The 26 `TailorPanel` behavioural tests already
 * cover the Gemini sentence's exact wording (AC-25, spec copy) and the accessible-description wiring
 * for the disabled reason — not repeated here. What is new: the checklist's shape (two items, each
 * naming what it is about) and that the disclosure is announced *by* the enabled button, which only
 * holds if it is wired before the button in the markup `useId` points at.
 */

const READY_CV: BaseCv = {
  id: '0192f0a1-aaaa-7000-8000-00000000aaaa',
  original_filename: 'jane-cv.pdf',
  content_type: 'application/pdf',
  size_bytes: 2048,
  status: 'extracted',
  character_count: 512,
  failure_reason: null,
  failure_message: null,
  uploaded_at: '2026-09-11T10:00:00Z',
  expires_at: '2026-09-12T10:00:00Z',
};

const READY_POSTING: JobPostingSummary = {
  id: '0192f0a1-bbbb-7000-8000-00000000bbbb',
  source: 'pasted',
  source_url: null,
  title: 'Senior Python Engineer',
  character_count: 4321,
  preview: 'We are looking for a senior Python engineer…',
  created_at: '2026-09-11T10:00:00Z',
  expires_at: '2026-09-12T10:00:00Z',
};

const READY_BASE_CV_CHECK: BaseCvCheck = { state: 'ready', baseCv: READY_CV };
const READY_JOB_POSTING_CHECK: JobPostingCheck = { state: 'ready', jobPosting: READY_POSTING };
const MISSING_BASE_CV_CHECK: BaseCvCheck = { state: 'missing' };
const MISSING_JOB_POSTING_CHECK: JobPostingCheck = { state: 'missing' };

describe('TailorLaunch', () => {
  it('renders the prerequisites as a two-item checklist naming the base CV and the job posting', () => {
    render(
      <TailorLaunch
        baseCv={READY_BASE_CV_CHECK}
        jobPosting={READY_JOB_POSTING_CHECK}
        hasPreviousRun={false}
        isStarting={false}
        onLaunch={vi.fn()}
      />,
    );

    const checklist = screen.getByRole('list', { name: 'What tailoring needs' });
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(checklist).toContainElement(items[0]);
    expect(checklist).toContainElement(items[1]);
    // Structure, not T42's exact detail wording: each item names its own subject.
    expect(items[0]).toHaveTextContent(/base cv/i);
    expect(items[1]).toHaveTextContent(/job posting/i);
  });

  it('names the CV file and the posting title in the checklist when both are ready', () => {
    render(
      <TailorLaunch
        baseCv={READY_BASE_CV_CHECK}
        jobPosting={READY_JOB_POSTING_CHECK}
        hasPreviousRun={false}
        isStarting={false}
        onLaunch={vi.fn()}
      />,
    );

    const items = screen.getAllByRole('listitem');
    expect(items[0]).toHaveTextContent(READY_CV.original_filename);
    expect(items[1]).toHaveTextContent(READY_POSTING.title ?? '');
  });

  it('positions the Gemini disclosure before the button in the document, and the enabled button is described by it', () => {
    render(
      <TailorLaunch
        baseCv={READY_BASE_CV_CHECK}
        jobPosting={READY_JOB_POSTING_CHECK}
        hasPreviousRun={false}
        isStarting={false}
        onLaunch={vi.fn()}
      />,
    );

    const button = screen.getByRole('button');
    expect(button).not.toBeDisabled();
    // T42-chosen structure: the disclosure text IS the enabled button's accessible description,
    // which only holds together if `aria-describedby` on the button resolves to an element that
    // exists — proving the wiring, not just that both strings are somewhere on the page.
    expect(button).toHaveAccessibleDescription(
      'Tailoring sends your CV text and this job posting to Google Gemini. Nothing else is sent.',
    );

    const disclosure = screen.getByText(
      'Tailoring sends your CV text and this job posting to Google Gemini. Nothing else is sent.',
    );
    const DOCUMENT_POSITION_FOLLOWING = 4;
    expect(disclosure.compareDocumentPosition(button) & DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('describes the disabled button with the blocked reason instead of the disclosure', () => {
    render(
      <TailorLaunch
        baseCv={MISSING_BASE_CV_CHECK}
        jobPosting={MISSING_JOB_POSTING_CHECK}
        hasPreviousRun={false}
        isStarting={false}
        onLaunch={vi.fn()}
      />,
    );

    const button = screen.getByRole('button');
    expect(button).toBeDisabled();
    const description = button.getAttribute('aria-describedby');
    expect(description).not.toBeNull();
    // Swapped, not merely present alongside: a screen-reader user on a disabled button hears why it
    // is disabled, not the (irrelevant, while disabled) Gemini disclosure.
    expect(button).not.toHaveAccessibleDescription(
      'Tailoring sends your CV text and this job posting to Google Gemini. Nothing else is sent.',
    );
  });

  it('renders no accessible name change between "Tailor my CV" and "Tailor again" beyond hasPreviousRun', () => {
    const { rerender } = render(
      <TailorLaunch
        baseCv={READY_BASE_CV_CHECK}
        jobPosting={READY_JOB_POSTING_CHECK}
        hasPreviousRun={false}
        isStarting={false}
        onLaunch={vi.fn()}
      />,
    );
    // T42-chosen copy (the spec does not fix this string): first run vs. repeat run reads
    // differently, and this is the boundary that decides which.
    expect(screen.getByRole('button', { name: 'Tailor my CV' })).toBeInTheDocument();

    rerender(
      <TailorLaunch
        baseCv={READY_BASE_CV_CHECK}
        jobPosting={READY_JOB_POSTING_CHECK}
        hasPreviousRun={true}
        isStarting={false}
        onLaunch={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: 'Tailor again' })).toBeInTheDocument();
  });
});
