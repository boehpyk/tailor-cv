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

  it('renders the closing it is given', () => {
    // F11 made `closing` a prop: `DocumentWorkspace`'s `EditorFallback` is the one production
    // caller since 1.4, and it passes E-29's sentence, not 1.3's "editing comes next" — the preview
    // is a read-only fallback after the bridge failed to open a document, not a promise of what
    // comes next. This is the shape every real caller now exercises.
    render(
      <TailoredDocumentsPreview
        tailoredCv={TAILORED_CV}
        coverLetter={COVER_LETTER}
        closing={<p role="alert">We couldn&apos;t open this document in the editor.</p>}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent(
      "We couldn't open this document in the editor.",
    );
    expect(
      screen.queryByText('This is a read-only preview. Editing and downloads come next.'),
    ).not.toBeInTheDocument();
  });

  it('falls back to the 1.3 sentence when no closing is given — test-only: no production caller passes none', () => {
    // T42-chosen wording, kept for the shape 1.3 shipped. `DocumentWorkspace` always supplies its
    // own `closing` (the test above), so this default is exercised only here; it stays as the
    // component's own honest default rather than a required prop, in case a future caller wants a
    // generic read-only preview with nothing special to say.
    render(<TailoredDocumentsPreview tailoredCv={TAILORED_CV} coverLetter={COVER_LETTER} />);

    expect(
      screen.getByText('This is a read-only preview. Editing and downloads come next.'),
    ).toBeInTheDocument();
  });

  it('renders each document pane as read-only — no textbox or other editable role', () => {
    render(<TailoredDocumentsPreview tailoredCv={TAILORED_CV} coverLetter={COVER_LETTER} />);

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
});
