import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';

import { DocumentTabs } from './DocumentTabs';

import type { SaveState } from '../saveState';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

/**
 * F12 — structure and markup for `DocumentTabs` (task-list F12, AC-30's marker): two `role="tab"`
 * links to the run's two documents, `aria-selected` on the current one, and an "unsaved" marker on
 * a tab whose document is not `saved`.
 *
 * A bare `MemoryRouter` is enough — `DocumentTabs` renders `<Link>`s (which throw outside a router
 * context, `NotFoundPage.tsx`'s reasoning) but does not itself navigate, so the real route table
 * (`renderWithRouter`) is more than this presentational-component test needs (the same call
 * `LatestRunCard.test.tsx` makes).
 */

const RUN_ID = 'run-abc-123';

const BOTH_SAVED: Readonly<Record<TailoredDocumentKind, SaveState>> = {
  cv: { kind: 'saved' },
  cover_letter: { kind: 'saved' },
};

function renderTabs(
  selected: TailoredDocumentKind,
  states: Readonly<Record<TailoredDocumentKind, SaveState>> = BOTH_SAVED,
): ReturnType<typeof render> {
  return render(
    <MemoryRouter>
      <DocumentTabs runId={RUN_ID} selected={selected} states={states} />
    </MemoryRouter>,
  );
}

function tab(name: RegExp): HTMLElement {
  return screen.getByRole('tab', { name });
}

describe('DocumentTabs', () => {
  it("renders two tabs linking to the run's CV and cover-letter documents", () => {
    renderTabs('cv');

    expect(tab(/^cv/i)).toHaveAttribute('href', `/runs/${RUN_ID}/cv`);
    expect(tab(/cover letter/i)).toHaveAttribute('href', `/runs/${RUN_ID}/cover_letter`);
  });

  it('marks only the current document aria-selected', () => {
    renderTabs('cover_letter');

    expect(tab(/^cv/i)).toHaveAttribute('aria-selected', 'false');
    expect(tab(/cover letter/i)).toHaveAttribute('aria-selected', 'true');
  });

  it('flips aria-selected when the current document changes', () => {
    renderTabs('cv');

    expect(tab(/^cv/i)).toHaveAttribute('aria-selected', 'true');
    expect(tab(/cover letter/i)).toHaveAttribute('aria-selected', 'false');
  });

  it('shows an unsaved marker only on the tab whose document is not saved (AC-30)', () => {
    renderTabs('cv', { cv: { kind: 'saved' }, cover_letter: { kind: 'dirty' } });

    expect(tab(/^cv/i)).not.toHaveTextContent(/unsaved/i);
    expect(tab(/cover letter/i)).toHaveTextContent(/unsaved/i);
  });

  it('shows no unsaved marker on either tab when both documents are saved', () => {
    renderTabs('cv');

    expect(tab(/^cv/i)).not.toHaveTextContent(/unsaved/i);
    expect(tab(/cover letter/i)).not.toHaveTextContent(/unsaved/i);
  });

  it('marks a tab unsaved for every non-saved state, not only "dirty"', () => {
    const savingStates: SaveState[] = [
      { kind: 'saving' },
      { kind: 'paused', retryAfterSeconds: 30 },
      { kind: 'expired' },
    ];

    for (const state of savingStates) {
      const { unmount } = renderTabs('cv', { cv: state, cover_letter: { kind: 'saved' } });
      expect(tab(/^cv/i)).toHaveTextContent(/unsaved/i);
      unmount();
    }
  });
});
