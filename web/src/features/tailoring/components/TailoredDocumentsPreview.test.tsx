import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { TailoredDocumentsPreview } from './TailoredDocumentsPreview';

import type { PreviewDocument } from './TailoredDocumentsPreview';

/**
 * T44 — structure and markup for `TailoredDocumentsPreview`: two labelled read-only panes, each
 * with its character count, and the "editing and downloads arrive next" sentence (task-list T44).
 * `TailorPanel.test.tsx`'s AC-31 test already covers the security property (a `<script>` body
 * renders as visible text, no `innerHTML`) and is not repeated here — this file is only the shape.
 */

const TAILORED_CV: PreviewDocument = { text: 'Jane Doe, Senior Engineer', characterCount: 777 };
const COVER_LETTER: PreviewDocument = {
  text: 'Dear Hiring Manager, I am writing to apply for this role.',
  characterCount: 888,
};

describe('TailoredDocumentsPreview', () => {
  it('renders two labelled regions, one per document', () => {
    render(<TailoredDocumentsPreview tailoredCv={TAILORED_CV} coverLetter={COVER_LETTER} />);

    // Spec copy (technical-plan.md's components table names these two panes): "Tailored CV" and
    // "Cover letter" are the section titles a `region` role exposes as its accessible name.
    const cvRegion = screen.getByRole('region', { name: 'Tailored CV' });
    const letterRegion = screen.getByRole('region', { name: 'Cover letter' });
    expect(cvRegion).not.toBe(letterRegion);
    expect(cvRegion).toHaveTextContent(TAILORED_CV.text);
    expect(letterRegion).toHaveTextContent(COVER_LETTER.text);
  });

  it("shows each pane its own character count, not the other one's", () => {
    render(<TailoredDocumentsPreview tailoredCv={TAILORED_CV} coverLetter={COVER_LETTER} />);

    const cvRegion = screen.getByRole('region', { name: 'Tailored CV' });
    const letterRegion = screen.getByRole('region', { name: 'Cover letter' });
    expect(cvRegion).toHaveTextContent('777 characters');
    expect(cvRegion).not.toHaveTextContent('888 characters');
    expect(letterRegion).toHaveTextContent('888 characters');
    expect(letterRegion).not.toHaveTextContent('777 characters');
  });

  it('omits a character count for a pane whose count is null', () => {
    render(
      <TailoredDocumentsPreview
        tailoredCv={{ ...TAILORED_CV, characterCount: null }}
        coverLetter={COVER_LETTER}
      />,
    );

    const cvRegion = screen.getByRole('region', { name: 'Tailored CV' });
    expect(cvRegion).not.toHaveTextContent(/characters/);
  });

  it('states that editing and downloads arrive next', () => {
    render(<TailoredDocumentsPreview tailoredCv={TAILORED_CV} coverLetter={COVER_LETTER} />);

    // T42-chosen wording — the technical-plan.md bullet only requires "one honest sentence that
    // editing and downloads arrive next", not this exact phrasing.
    expect(
      screen.getByText('This is a read-only preview. Editing and downloads come next.'),
    ).toBeInTheDocument();
  });

  it('renders each document pane as read-only — no textbox or other editable role', () => {
    render(<TailoredDocumentsPreview tailoredCv={TAILORED_CV} coverLetter={COVER_LETTER} />);

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
});
